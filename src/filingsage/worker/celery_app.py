"""Celery application — the transport layer of our event-driven pipeline.

Design stance (spec §3): event-driven *architecture*, queue *transport*.
Pipeline steps emit events and chain tasks; Redpanda/Kafka slots into this
seam later only if volume ever justifies it.
"""

from celery import Celery
from celery.schedules import crontab

from filingsage.config import get_settings

settings = get_settings()

celery_app = Celery(
    "filingsage",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # recovery.py defines the reconciler task; it imports tasks.py, so it
    # can't live there without a circular import.
    include=["filingsage.worker.tasks", "filingsage.worker.recovery"],
)

celery_app.conf.update(
    # At-least-once semantics: ack only after the task finishes, so a worker
    # crash mid-ingestion requeues the filing instead of silently losing it.
    # Safe because pipeline tasks will be idempotent (keyed by accession no.,
    # bronze writes are immutable).
    task_acks_late=True,
    # Long, uneven task durations (fetching/parsing filings) → don't let one
    # worker hoard a prefetched backlog while others sit idle.
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    # --- Upstash free-tier quota incident (README → Technical Decisions #25) ---
    # 500K commands/month exhausted almost entirely by broker polling
    # overhead, not real work: 478k reads vs 24k writes over several days of
    # an idle worker running a handful of tasks per 2h cron cycle. Tuned for
    # that traffic pattern — low volume, single always-on worker, no need
    # for sub-second responsiveness.
    #
    # Kombu's redis transport polls Redis for new messages at this interval
    # when the queue is idle; its default is sub-second, which is correct
    # for a busy broker and wasteful for ours. A few seconds of extra pickup
    # latency is irrelevant for a cron-triggered workload, so this is a
    # straightforward win with no real tradeoff for us.
    broker_transport_options={"polling_interval": 2.0},
    # Detects a silently-dropped connection to the broker (e.g. a managed
    # Redis proxy killing a long-idle TCP connection) — NOT disabled
    # outright: this worker sits idle for up to ~2h between cron bursts,
    # exactly the scenario where an undetected dead connection would bite
    # hardest (a missed cron trigger with no automatic reconnect until the
    # next task is pushed). Loosened from Celery's default of 120s (checked
    # ~3x per interval, so effectively every ~40s) to 300s (~every 100s) —
    # meaningfully fewer checks, still frequent enough to catch a dead
    # connection well within a 2h window.
    broker_heartbeat=300,
    # --- Scheduled ingestion (Technical Decisions #31) ---
    # Celery beat replaces the GitHub Actions cron for the localhost stack:
    # a cron on GitHub's runners can't reach an API on localhost, and the
    # spec's own §2 diagram already names "Celery workers + beat". Same
    # cadence as the old cron, and the same off-the-hour minute (:17) so a
    # future hosted deployment can flip back without changing behavior.
    # The schedule names a task, not its arguments: scheduled_ingest
    # resolves WHICH tickers at run time, so the list can later come from
    # users' watchlists without restarting beat.
    beat_schedule={
        "scheduled-ingest-every-2h": {
            "task": "filingsage.scheduled_ingest",
            "schedule": crontab(minute=17, hour="*/2"),
        },
        # Decision #37: re-enqueue filings that stopped making progress. Its
        # stuck threshold is also 30 minutes, so a filing is picked up at
        # most ~an hour after it stalled.
        "reconcile-pipeline-every-30m": {
            "task": "filingsage.reconcile_pipeline",
            "schedule": crontab(minute="*/30"),
        },
    },
    timezone="UTC",
)
