"""FastAPI application entrypoint."""

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from filingsage import __version__
from filingsage.api.rate_limit import (
    RATE_LIMIT_MAX_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    check_rate_limit,
)
from filingsage.config import get_settings
from filingsage.gold.qa import Answer, answer_question
from filingsage.worker.tasks import ingest_watchlist

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Once, at startup, not per-request — a standing reminder in `fly logs`
    # for as long as this remains true. See ask_question()'s docstring for
    # the full reasoning.
    logger.warning(
        "POST /qa has NO AUTHENTICATION — intentional (real JWT auth is Week "
        "3 scope, spec line 218), not an oversight. Protected only by a "
        "per-IP rate limit (%d req / %ds).",
        RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS,
    )
    yield


app = FastAPI(
    title="FilingSage",
    version=__version__,
    description=(
        "An AI research analyst that watches the companies you care about, "
        "reads every new SEC filing the moment it drops, and answers with citations."
    ),
    lifespan=lifespan,
)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    """Liveness probe: 'this process is up and serving requests'.

    Deliberately does NOT check Postgres/Redis — that's a readiness concern,
    and conflating the two makes orchestrators restart a healthy app because
    a dependency blipped. A /readyz with dependency checks lands with the
    DB layer in Week 1.
    """
    settings = get_settings()
    return {"status": "ok", "service": "filingsage-api", "version": __version__, "env": settings.env}


class IngestRequest(BaseModel):
    tickers: list[str] | None = None
    limit: int | None = None


@app.post("/internal/ingest", status_code=202, tags=["ops"])
def trigger_ingest(
    body: IngestRequest | None = None, x_ingest_token: str | None = Header(default=None)
) -> dict:
    """Enqueue ingest_watchlist — the endpoint the GitHub Actions cron hits every 2h.

    Auth is a shared secret, not a full auth stack: this is a single
    machine-to-machine trigger, not a user-facing route (real JWT auth lands
    Week 3 for user-facing endpoints). An unset ingest_token fails closed
    (503) rather than silently accepting any request — an empty secret must
    never mean "no auth required". A configured-but-wrong/missing token is a
    401, checked with constant-time comparison to avoid a timing side channel
    on the secret.
    """
    settings = get_settings()
    if not settings.ingest_token:
        raise HTTPException(status_code=503, detail="ingest trigger not configured")
    if not x_ingest_token or not secrets.compare_digest(x_ingest_token, settings.ingest_token):
        raise HTTPException(status_code=401, detail="invalid or missing ingest token")

    body = body or IngestRequest()
    tickers = body.tickers if body.tickers is not None else settings.default_universe
    result = ingest_watchlist.delay(tickers, body.limit)
    return {"task_id": result.id}


class QARequest(BaseModel):
    # max_length=500: generous for any real question, short enough that
    # someone can't paste a whole document in as "a question" and blow up
    # LLM context/cost.
    question: str = Field(min_length=1, max_length=500)
    ticker: str | None = None
    form_type: str | None = None
    since: date | None = None


def _client_ip(request: Request) -> str:
    """Real client IP through Fly's edge — no Cloudflare Tunnel in front of
    this app (that was the pre-Fly Oracle VM plan, README → Technical
    Decisions #21; docker-compose.prod.yml's Cloudflare comment is a leftover
    from that superseded plan, not something this app actually sits behind).
    Fly's own edge/proxy terminates TLS directly (deploy/api/fly.toml's
    [http_service]).

    Verified directly against the live deployment (temporary /internal/
    debug-headers route, curled from outside Fly's network, then removed):
      - Fly-Client-IP: exactly one value, the real originating connection
        IP. Sending a forged `Fly-Client-IP` header myself had no effect —
        Fly's edge overwrites it unconditionally with what it actually
        observed. Trustworthy.
      - X-Forwarded-For: sending a forged `X-Forwarded-For: 1.2.3.4` got
        PREPENDED ("1.2.3.4, <real ip>, <fly internal hop>") rather than
        replaced or rejected — a naive first-entry read is fully
        attacker-controlled. NOT trustworthy for anything security-relevant
        (this rate limit included) on this deployment.
      - request.client.host: always Fly's internal per-machine proxy
        sidecar (a 172.16.x.x address), never the real visitor, with or
        without Cloudflare in the picture. Kept only as a last-resort
        fallback for non-Fly environments (local dev) where neither header
        is present at all.
    """
    fly_client_ip = request.headers.get("fly-client-ip")
    if fly_client_ip:
        return fly_client_ip
    return request.client.host if request.client else "unknown"


@app.post("/qa", tags=["qa"])
def ask_question(body: QARequest, request: Request) -> Answer:
    """Cited Q&A over embedded filing chunks (spec §6 step 4).

    NO AUTH YET — intentional, not an oversight: this is the first
    public-facing endpoint (/internal/ingest's shared-secret header is
    machine-to-machine only, not a model for a user-facing route), and
    real JWT auth is explicit Week 3 scope (spec line 218). Until then,
    the only protection is the per-IP rate limit below, plus a startup-time
    warning log (this module's lifespan handler) so the gap stays visible
    in `fly logs`, not forgotten.

    check_rate_limit() itself is wrapped in its own try/except, separate
    from answer_question()'s below — a production bug showed this endpoint
    returning a raw 500 (not the clean 503 below) because the rate limiter
    call sat OUTSIDE the try/except entirely, so its own failure (Redis
    unreachable/misconfigured) bypassed all error handling. FAIL CLOSED
    here (503, not "skip the check and serve anyway"): this endpoint has no
    other auth, so the rate limit is the only cap on cost/abuse exposure —
    serving requests unchecked during exactly the moment that cap breaks is
    the wrong failure mode for an unauthenticated, LLM-backed endpoint.
    """
    try:
        allowed = check_rate_limit(_client_ip(request))
    except Exception:
        logger.exception("qa: rate limiter failed — failing closed (no other auth on this endpoint)")
        raise HTTPException(status_code=503, detail="Q&A is temporarily unavailable") from None

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded — max {RATE_LIMIT_MAX_REQUESTS} "
                f"requests per {RATE_LIMIT_WINDOW_SECONDS}s."
            ),
        )

    try:
        return answer_question(
            body.question, ticker=body.ticker, form_type=body.form_type, since=body.since
        )
    except Exception:
        # Never leak internals (LLM/Qdrant errors, stack traces) to the
        # caller — log the real exception server-side, return a generic 503.
        logger.exception("qa: answer_question failed")
        raise HTTPException(status_code=503, detail="Q&A is temporarily unavailable") from None
