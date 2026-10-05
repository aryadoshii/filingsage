"""Which pipeline failures are worth retrying, and how long to wait between tries.

Pure functions, no Celery or database, so every rule has a unit test
(tests/test_retry.py). worker/tasks.py applies them; the reconciler
(worker/recovery.py) decides what happens after the last retry.

Transient = the same call could succeed if made again later: the network
dropped, a server was briefly overloaded, a database connection was lost.
Everything else is deterministic for the same input — a 404, a parse
quarantine, a missing bronze/silver file, a bug — and retrying it would only
reproduce the failure and spend SEC rate budget doing it.
"""

from __future__ import annotations

import random
from collections.abc import Callable

import httpx
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.exc import OperationalError

# Retries after the first attempt, so a task runs at most 5 times.
MAX_RETRIES = 4
RETRY_BASE_SECONDS = 30.0
ERROR_MAX_CHARS = 500


def _retryable_status(status: int | None) -> bool:
    return status is not None and (status == 429 or status >= 500)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        # EDGAR: 429 and 5xx are "try later"; any other 4xx (404, and 403
        # once EdgarClient's own backoff has given up) won't change on retry.
        return _retryable_status(exc.response.status_code)
    if isinstance(exc, httpx.TransportError):  # connect/read errors and timeouts
        return True
    if isinstance(exc, ResponseHandlingException):
        # qdrant-client wraps both "couldn't reach the server" and "couldn't
        # parse its reply" in this one type; only the first is transient.
        return isinstance(exc.source, httpx.TransportError | OSError)
    if isinstance(exc, UnexpectedResponse):  # Qdrant answered with an HTTP error
        return _retryable_status(exc.status_code)
    if isinstance(exc, OperationalError):  # Postgres connection lost or refused
        return True
    return False


def retry_delay(retries_so_far: int, *, rand: Callable[[], float] = random.random) -> float:
    """Exponential backoff plus jitter: 30, 60, 120, 240 s, each plus up to
    30 s at random so failures that happened together don't retry together
    (e.g. both worker processes hitting the same EDGAR outage)."""
    return RETRY_BASE_SECONDS * 2**retries_so_far + rand() * RETRY_BASE_SECONDS


def describe_error(exc: BaseException) -> str:
    """'ExceptionClass: message', truncated — what a failure event records."""
    return f"{type(exc).__name__}: {exc}"[:ERROR_MAX_CHARS]
