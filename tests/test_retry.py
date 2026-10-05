"""Retry policy (worker/retry.py) and the task-side decision that applies it
(tasks._retry_if_transient). Pure unit tests: no broker, no database."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.exc import OperationalError

from filingsage.parsing.silver import ParseQuarantineError
from filingsage.worker import tasks
from filingsage.worker.retry import (
    ERROR_MAX_CHARS,
    MAX_RETRIES,
    describe_error,
    is_transient,
    retry_delay,
)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://www.sec.gov/Archives/x.htm")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


def _qdrant_status(status: int) -> UnexpectedResponse:
    return UnexpectedResponse(status, "reason", b"", httpx.Headers())


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        _status_error(429),
        _status_error(500),
        _status_error(503),
        ResponseHandlingException(httpx.ConnectError("qdrant down")),
        ResponseHandlingException(ConnectionRefusedError()),
        _qdrant_status(503),
        OperationalError("SELECT 1", {}, Exception("server closed the connection")),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transient_errors_are_retried(exc):
    assert is_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(404),
        _status_error(403),  # EdgarClient already backed off on it before raising
        _status_error(400),
        ResponseHandlingException(ValueError("couldn't parse the response")),
        _qdrant_status(400),
        ParseQuarantineError("no sections found"),
        FileNotFoundError("data/bronze/x.htm"),
        ValueError("a bug"),
        RuntimeError("anything else"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_deterministic_errors_are_not_retried(exc):
    assert not is_transient(exc)


def test_backoff_doubles_with_bounded_jitter():
    assert [retry_delay(n, rand=lambda: 0.0) for n in range(MAX_RETRIES)] == [30, 60, 120, 240]
    assert [retry_delay(n, rand=lambda: 1.0) for n in range(MAX_RETRIES)] == [60, 90, 150, 270]


def test_error_descriptions_name_the_class_and_are_truncated():
    assert describe_error(FileNotFoundError("gone")) == "FileNotFoundError: gone"
    long = describe_error(ValueError("x" * 2_000))
    assert long.startswith("ValueError: xxx")
    assert len(long) == ERROR_MAX_CHARS


# --- tasks._retry_if_transient ------------------------------------------------


class _Retrying(Exception):
    """Stands in for celery.exceptions.Retry."""


class _FakeTask:
    name = "filingsage.fake"

    def __init__(self, retries: int = 0, called_directly: bool = False):
        self.request = SimpleNamespace(retries=retries, called_directly=called_directly)
        self.countdowns: list[float] = []

    def retry(self, exc, countdown):
        self.countdowns.append(countdown)
        return _Retrying()


def test_a_transient_error_with_retries_left_schedules_a_retry():
    task = _FakeTask(retries=1)
    with pytest.raises(_Retrying):
        tasks._retry_if_transient(task, httpx.ConnectError("down"))
    assert len(task.countdowns) == 1 and 60 <= task.countdowns[0] <= 90


def test_the_last_attempt_gives_up_and_reports_every_attempt():
    task = _FakeTask(retries=MAX_RETRIES)
    assert tasks._retry_if_transient(task, httpx.ConnectError("down")) == MAX_RETRIES + 1
    assert task.countdowns == []


def test_a_deterministic_error_gives_up_immediately():
    task = _FakeTask(retries=0)
    assert tasks._retry_if_transient(task, FileNotFoundError("gone")) == 1
    assert task.countdowns == []


def test_a_task_called_as_a_plain_function_never_retries():
    """The CLI calls refresh_company directly: there's no broker to re-queue
    through, so it fails like a normal function call would."""
    task = _FakeTask(retries=0, called_directly=True)
    assert tasks._retry_if_transient(task, httpx.ConnectError("down")) == 1
    assert task.countdowns == []
