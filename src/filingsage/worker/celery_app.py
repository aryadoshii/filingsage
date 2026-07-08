"""Celery application — the transport layer of our event-driven pipeline.

Design stance (spec §3): event-driven *architecture*, queue *transport*.
Pipeline steps emit events and chain tasks; Redpanda/Kafka slots into this
seam later only if volume ever justifies it.
"""

from celery import Celery

from filingsage.config import get_settings

settings = get_settings()

celery_app = Celery(
    "filingsage",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["filingsage.worker.tasks"],
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
)
