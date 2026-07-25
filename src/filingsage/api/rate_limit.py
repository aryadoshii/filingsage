"""Redis-backed per-IP rate limiting for public endpoints.

Currently just /qa (api/main.py) — see that route's docstring for why it
has no auth yet and relies on this instead.

Fixed-window counter, not sliding-window/token-bucket: a fixed window can
let a caller send up to ~2x the nominal rate right at a window boundary —
a well-understood tradeoff that's fine for "stop trivial abuse," not
"enforce an exact quota." Hand-rolled (~15 lines) rather than pulling in a
library (e.g. slowapi) for the same reason EdgarClient's own rate limiter
is hand-rolled (README → Technical Decisions #14): fully explainable in one
read, and Redis is already a dependency (Celery's broker) — no new one
needed.
"""

from __future__ import annotations

import time
from functools import lru_cache

from redis import Redis

from filingsage.config import get_settings

# Generous but real: a genuine user asking several follow-up questions in a
# minute is unaffected; a script hammering the endpoint to run up a Groq/
# Gemini bill (or just DoS the worker with LLM calls) is not.
RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def check_rate_limit(client_ip: str, *, redis_client: Redis | None = None) -> bool:
    """True if `client_ip` is still within budget for the current window,
    False if it has exceeded RATE_LIMIT_MAX_REQUESTS.

    Key is IP + the window's own start (unix time // window length), so
    the counter naturally expires — no separate cleanup job — and the
    Redis TTL is set once per window (on the first request in it), not on
    every call.
    """
    r = redis_client or _redis_client()
    window_start = int(time.time()) // RATE_LIMIT_WINDOW_SECONDS
    key = f"qa:ratelimit:{client_ip}:{window_start}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    return count <= RATE_LIMIT_MAX_REQUESTS
