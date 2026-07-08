"""Celery tasks. Day 1: a single ping task to prove the broker round-trip."""

from filingsage.worker.celery_app import celery_app


@celery_app.task(name="filingsage.ping")
def ping() -> str:
    """Round-trip smoke test: API container -> Redis -> worker -> Redis -> caller."""
    return "pong"
