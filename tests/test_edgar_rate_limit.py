"""The shared EDGAR rate limit (connectors/rate_limit.py, decision #36).

Real Redis via testcontainers for everything that's about Redis — the
property under test is that separate clients (standing in for separate
processes) share one budget, which a fake Redis can't prove. Time is a
shared fake clock where exact window arithmetic matters, and the real clock
plus real threads where atomicity under concurrency matters.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from testcontainers.redis import RedisContainer

import filingsage.cli as cli
import filingsage.worker.tasks as tasks
from filingsage.connectors import rate_limit
from filingsage.connectors.rate_limit import (
    EDGAR_MAX_PER_SECOND,
    REPROBE_SECONDS,
    SharedRateLimiter,
)
from filingsage.redis_client import redis_from_url

BUDGET = EDGAR_MAX_PER_SECOND


class FakeTime:
    """One clock for every limiter in a test; sleeping advances it."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _max_in_any_window(times: list[float], window: float = 1.0) -> int:
    """Most grants inside any rolling (t - window, t] interval."""
    times = sorted(times)
    worst, start = 0, 0
    for end, t in enumerate(times):
        while times[start] <= t - window:
            start += 1
        worst = max(worst, end - start + 1)
    return worst


@pytest.fixture(scope="module")
def redis_container():
    with RedisContainer() as container:
        yield container


@pytest.fixture
def key() -> str:
    return f"edgar:rate:test:{uuid.uuid4().hex}"  # tests never share a window


# --- shared budget (real Redis) -----------------------------------------------


@pytest.mark.integration
def test_two_limiters_sharing_redis_never_exceed_the_budget_in_any_second(redis_container, key):
    clock = FakeTime()
    limiters = [
        SharedRateLimiter(redis_container.get_client(), key=key, clock=clock.time, sleep=clock.sleep)
        for _ in range(2)
    ]
    grants: list[float] = []
    for i in range(40):
        limiters[i % 2].wait()
        grants.append(clock.now)
        clock.now += 0.03  # each request takes a little time

    assert _max_in_any_window(grants) == BUDGET
    # Not just "never too many": the budget is actually used. 40 requests at
    # 8/s need at least (40 - 8) / 8 = 4 seconds after the first burst.
    assert grants[-1] - grants[0] == pytest.approx(4.0, abs=0.5)


@pytest.mark.integration
def test_a_burst_across_a_second_boundary_is_still_capped(redis_container, key):
    """The case a per-second counter key gets wrong: a full budget at the
    end of one calendar second, then another at the start of the next."""
    clock = FakeTime(start=1_000.9)
    limiter = SharedRateLimiter(
        redis_container.get_client(), key=key, clock=clock.time, sleep=clock.sleep
    )
    grants = []
    for _ in range(BUDGET + 1):
        limiter.wait()
        grants.append(clock.now)
        clock.now += 0.01

    assert grants[BUDGET - 1] < 1_001.0              # the first 8 fit before the boundary
    assert grants[BUDGET] >= grants[0] + 1.0 - 1e-6  # the 9th waits a full second, boundary or not


@pytest.mark.integration
def test_concurrent_threads_with_separate_clients_share_one_budget(redis_container, key):
    """Real clock, real threads, one Redis client each — the closest
    stand-in for two worker processes — racing for the same slots."""
    grants: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        last_read = [0.0]

        def clock() -> float:
            last_read[0] = time.time()
            return last_read[0]

        limiter = SharedRateLimiter(redis_container.get_client(), key=key, clock=clock)
        for _ in range(12):
            limiter.wait()
            with lock:
                grants.append(last_read[0])  # the timestamp the grant was scored at

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(grants) == 24
    assert _max_in_any_window(grants) <= BUDGET


# --- Redis unreachable -----------------------------------------------------------


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.integration
def test_falls_back_to_a_slow_in_process_limit_when_redis_is_down(caplog):
    clock = FakeTime()
    dead = Redis(host="127.0.0.1", port=_closed_port(), socket_connect_timeout=0.5)
    limiter = SharedRateLimiter(dead, clock=clock.time, sleep=clock.sleep)
    grants = []
    with caplog.at_level(logging.WARNING, logger=rate_limit.__name__):
        for _ in range(5):
            limiter.wait()  # never raises: ingestion keeps going
            grants.append(clock.now)

    assert limiter.using_fallback
    assert grants == pytest.approx([1_000.0, 1_000.5, 1_001.0, 1_001.5, 1_002.0])  # 2 req/s
    warnings = [r for r in caplog.records if "Redis unreachable" in r.getMessage()]
    assert len(warnings) == 1  # once per outage, not once per request


class _Switchable:
    """A real Redis client that can be made to fail on demand."""

    def __init__(self, inner: Redis):
        self.inner = inner
        self.down = True

    def pipeline(self, *args, **kwargs):
        if self.down:
            raise RedisConnectionError("simulated outage")
        return self.inner.pipeline(*args, **kwargs)

    def zrem(self, *args):
        return self.inner.zrem(*args)


@pytest.mark.integration
def test_returns_to_the_shared_budget_once_redis_is_back(redis_container, key):
    clock = FakeTime()
    client = _Switchable(redis_container.get_client())
    limiter = SharedRateLimiter(client, key=key, clock=clock.time, sleep=clock.sleep)

    limiter.wait()
    assert limiter.using_fallback

    client.down = False
    clock.now += REPROBE_SECONDS  # the limiter retries Redis only after this
    limiter.wait()
    assert not limiter.using_fallback
    assert redis_container.get_client().zcard(key) == 1  # the grant was recorded in Redis


# --- wiring -------------------------------------------------------------------------


class _Settings:
    sec_contact_email = "arya@test.dev"
    redis_url = "redis://localhost:6379/0"

    def __init__(self, tmp_path):
        self.bronze_dir = tmp_path / "bronze"


@pytest.mark.parametrize("module, build", [(tasks, "_connector"), (cli, "_build_connector")])
def test_every_edgar_client_the_app_builds_uses_the_shared_limiter(module, build, tmp_path, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(module, "shared_edgar_limiter", lambda: sentinel)
    monkeypatch.setattr(module, "get_settings", lambda: _Settings(tmp_path))

    connector = getattr(module, build)()

    assert connector._client._limiter is sentinel


def test_redis_url_rewrite_for_tls_urls():
    client = redis_from_url(
        "rediss://default:pw@example.upstash.io:6379?ssl_cert_reqs=CERT_REQUIRED",
        socket_timeout=1.0,
    )
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["ssl_cert_reqs"] == "required"
    assert kwargs["socket_timeout"] == 1.0


def test_plain_redis_urls_pass_through_unchanged():
    kwargs = redis_from_url("redis://localhost:6379/0").connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("localhost", 6379, 0)
    assert "ssl_cert_reqs" not in kwargs
