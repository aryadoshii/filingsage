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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from redis import Redis

from filingsage.config import get_settings

# Generous but real: a genuine user asking several follow-up questions in a
# minute is unaffected; a script hammering the endpoint to run up a Groq/
# Gemini bill (or just DoS the worker with LLM calls) is not.
RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    """Production bug, fixed here: REDIS_URL's `?ssl_cert_reqs=CERT_REQUIRED`
    query param works for Celery/kombu's redis transport, but NOT for a
    direct redis-py `Redis.from_url()` call like this one — confirmed by
    reading redis-py's own source (redis.connection.parse_url /
    SSLConnection.__init__): `ssl_cert_reqs` isn't in
    URL_QUERY_ARGUMENT_PARSERS, so the literal query-string value is passed
    straight through, and SSLConnection only accepts the lowercase strings
    "none"/"optional"/"required" — "CERT_REQUIRED" isn't one of them, and
    redis-py raises exactly the `RedisError: Invalid SSL Certificate
    Requirements Flag: CERT_REQUIRED` seen in production the moment this
    endpoint took its first real request.

    Can't just pass the correct value as a kwarg alongside the URL either:
    `ConnectionPool.from_url()` documents that "querystring arguments
    always win" over conflicting kwargs, specifically so a URL's own
    settings can't be silently overridden — so the bad query param has to
    be stripped from the URL before redis-py ever parses it. REDIS_URL
    itself is left untouched (it's shared with Celery's broker connection,
    which does handle "CERT_REQUIRED" correctly) — only this module's own
    parsing of it changes.
    """
    url = get_settings().redis_url
    parsed = urlparse(url)
    kwargs: dict[str, str] = {}
    if parsed.scheme == "rediss":
        query = parse_qs(parsed.query)
        query.pop("ssl_cert_reqs", None)
        parsed = parsed._replace(query=urlencode(query, doseq=True))
        kwargs["ssl_cert_reqs"] = "required"
    return Redis.from_url(urlunparse(parsed), **kwargs)


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
