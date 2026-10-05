"""One way to open a direct redis-py connection from REDIS_URL.

Shared by the /qa rate limiter (api/rate_limit.py), the EDGAR rate limiter
(connectors/rate_limit.py) and /readyz — everything that talks to Redis
itself rather than through Celery's broker connection.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from redis import Redis


def redis_from_url(url: str, **kwargs: Any) -> Redis:
    """Production bug, fixed here: REDIS_URL's `?ssl_cert_reqs=CERT_REQUIRED`
    query param works for Celery/kombu's redis transport, but NOT for a
    direct redis-py `Redis.from_url()` call like this one — confirmed by
    reading redis-py's own source (redis.connection.parse_url /
    SSLConnection.__init__): `ssl_cert_reqs` isn't in
    URL_QUERY_ARGUMENT_PARSERS, so the literal query-string value is passed
    straight through, and SSLConnection only accepts the lowercase strings
    "none"/"optional"/"required" — "CERT_REQUIRED" isn't one of them, and
    redis-py raises exactly the `RedisError: Invalid SSL Certificate
    Requirements Flag: CERT_REQUIRED` seen in production the moment /qa
    took its first real request.

    Can't just pass the correct value as a kwarg alongside the URL either:
    `ConnectionPool.from_url()` documents that "querystring arguments
    always win" over conflicting kwargs, specifically so a URL's own
    settings can't be silently overridden — so the bad query param has to
    be stripped from the URL before redis-py ever parses it. REDIS_URL
    itself is left untouched (it's shared with Celery's broker connection,
    which does handle "CERT_REQUIRED" correctly) — only this parsing of it
    changes. A plain redis:// URL (the localhost stack) passes through as-is.
    """
    parsed = urlparse(url)
    if parsed.scheme == "rediss":
        query = parse_qs(parsed.query)
        query.pop("ssl_cert_reqs", None)
        parsed = parsed._replace(query=urlencode(query, doseq=True))
        kwargs["ssl_cert_reqs"] = "required"
    return Redis.from_url(urlunparse(parsed), **kwargs)
