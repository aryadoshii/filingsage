"""Scheduled ingestion (Celery beat) — the localhost replacement for the
GitHub Actions cron. No broker, no worker: beat's schedule is plain config,
and scheduled_ingest is called directly as a function.
"""

from types import SimpleNamespace

from celery.schedules import crontab

import filingsage.worker.recovery  # noqa: F401 — registers the reconciler task
from filingsage.worker import tasks
from filingsage.worker.celery_app import celery_app


def test_beat_schedule_points_at_a_registered_task():
    """A typo in the schedule's task name fails silently at runtime (beat
    sends a message no worker knows how to run) — catch it here instead."""
    entry = celery_app.conf.beat_schedule["scheduled-ingest-every-2h"]
    assert entry["task"] in celery_app.tasks
    assert entry["schedule"] == crontab(minute=17, hour="*/2")


def test_the_reconciler_runs_every_30_minutes():
    entry = celery_app.conf.beat_schedule["reconcile-pipeline-every-30m"]
    assert entry["task"] in celery_app.tasks
    assert entry["schedule"] == crontab(minute="*/30")


def test_the_worker_loads_the_module_that_defines_the_reconciler():
    """beat only sends the task's name; the worker imports `include` at
    startup — a module missing there means "unregistered task" at runtime."""
    assert "filingsage.worker.recovery" in celery_app.conf.include


def test_scheduled_ingest_resolves_tickers_at_run_time(monkeypatch):
    """...and enqueues ingest_watchlist through the broker rather than
    calling it in-process, so the scheduled run gets its retry policy."""
    calls: list = []
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(default_universe=["AAPL", "MSFT"], ingest_limit_per_ticker=7),
    )
    monkeypatch.setattr(
        tasks.ingest_watchlist,
        "delay",
        lambda tickers, limit: calls.append((tickers, limit)) or SimpleNamespace(id="task-1"),
    )

    result = tasks.scheduled_ingest()

    assert calls == [(["AAPL", "MSFT"], 7)]
    assert result == {"task_id": "task-1"}
