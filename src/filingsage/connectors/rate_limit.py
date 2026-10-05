"""One SEC request budget shared by every process that talks to EDGAR.

SEC's fair-access policy caps a requester at 10 requests/second. EdgarClient's
own RateLimiter enforces a rate per *process*, but the pipeline runs several:
two Celery worker processes (--concurrency=2), plus the CLI when it's used.
Each task builds its own EdgarClient, so per-process limiters at 8 req/s
could add up to 16 req/s across the two workers alone (decision #36). This
limiter keeps the count in Redis — already in the stack as Celery's broker —
so the 8 req/s budget is global.

Sliding window, not fixed: a per-second counter key ("edgar:rate:<second>")
lets a full budget through at the end of one second and another full budget
at the start of the next — 16 requests inside a fraction of a second. Here
every granted request is a member of one sorted set, scored by its
timestamp, and a request is granted only if fewer than `max_per_second`
members fall in the last second. Prune + add + count run in one MULTI/EXEC
transaction, so two processes can never both see room for the last slot.

If Redis is unreachable, the limiter falls back to an in-process limiter at
FALLBACK_PER_SECOND and logs a warning: ingestion keeps going, slower. Only
processes that call EDGAR count toward the total (the two worker processes
and an occasional CLI run; beat and the API only enqueue tasks), so even
with all of them on the fallback the combined rate stays under SEC's 10.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError

from filingsage.config import get_settings
from filingsage.connectors.edgar import RateLimiter
from filingsage.redis_client import redis_from_url

logger = logging.getLogger(__name__)

EDGAR_MAX_PER_SECOND = 8      # global budget: headroom under SEC's 10/s
FALLBACK_PER_SECOND = 2.0     # per process, only while Redis is unreachable
WINDOW_SECONDS = 1.0
REDIS_KEY = "edgar:rate"
# While on the fallback, try Redis again this often rather than on every
# request — an unreachable host can cost a connect timeout per attempt.
REPROBE_SECONDS = 30.0


class SharedRateLimiter:
    """At most `max_per_second` requests in any rolling one-second window,
    summed over every process using the same Redis and key.

    Same `wait()` interface as RateLimiter, so EdgarClient takes either.
    `clock` and `sleep` are injectable so tests control time; `clock` must be
    wall-clock time shared by all processes (every EDGAR caller runs in the
    same Docker VM, so they read the same clock).
    """

    def __init__(
        self,
        redis: Redis,
        max_per_second: int = EDGAR_MAX_PER_SECOND,
        *,
        key: str = REDIS_KEY,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        fallback: RateLimiter | None = None,
    ):
        self._redis = redis
        self._budget = max_per_second
        self._key = key
        self._clock = clock
        self._sleep = sleep
        self._fallback = fallback or RateLimiter(FALLBACK_PER_SECOND, sleep=sleep, clock=clock)
        self._fallback_until: float | None = None  # set while Redis is unreachable

    @property
    def using_fallback(self) -> bool:
        return self._fallback_until is not None

    def wait(self) -> None:
        while True:
            now = self._clock()
            if self._fallback_until is not None and now < self._fallback_until:
                self._fallback.wait()
                return
            member = uuid.uuid4().hex  # unique even when two callers read the same clock value
            try:
                pipe = self._redis.pipeline()  # transaction=True: MULTI ... EXEC
                pipe.zremrangebyscore(self._key, "-inf", now - WINDOW_SECONDS)
                pipe.zadd(self._key, {member: now})
                pipe.zcard(self._key)
                pipe.zrange(self._key, 0, 0, withscores=True)
                pipe.expire(self._key, 2)  # an idle key cleans itself up
                _, _, count, oldest, _ = pipe.execute()
            except RedisError as exc:
                if self._fallback_until is None:
                    logger.warning(
                        "EDGAR rate limiter: Redis unreachable (%s: %s); falling back to "
                        "%.0f req/s in this process until it's back",
                        type(exc).__name__, exc, FALLBACK_PER_SECOND,
                    )
                self._fallback_until = now + REPROBE_SECONDS
                self._fallback.wait()
                return

            if self._fallback_until is not None:
                logger.info("EDGAR rate limiter: Redis reachable again; shared budget restored")
                self._fallback_until = None
            if count <= self._budget:
                return
            # Over budget: give the slot back, then wait until the oldest
            # request in the window ages out and frees one.
            try:
                self._redis.zrem(self._key, member)
            except RedisError:
                pass  # it expires with the window anyway; counting it only slows others
            oldest_at = oldest[0][1] if oldest else now
            self._sleep(min(max(oldest_at + WINDOW_SECONDS - now, 0.001), WINDOW_SECONDS))


@lru_cache(maxsize=1)
def shared_edgar_limiter() -> SharedRateLimiter:
    """One limiter per process, reused by every EdgarClient that process
    builds — tasks build a new client per call, and the fallback limiter's
    pacing must carry across them. Short socket timeouts so an unreachable
    Redis costs a second, not a hang, before the fallback takes over."""
    client = redis_from_url(
        get_settings().redis_url, socket_connect_timeout=1.0, socket_timeout=1.0
    )
    return SharedRateLimiter(client)
