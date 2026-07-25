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
    """Best-effort real client IP behind Fly + Cloudflare Tunnel.

    Prefers X-Forwarded-For's first entry (the original client, per the
    standard chained-proxy convention) over request.client.host, which
    behind a tunnel is the tunnel/proxy's own address, not the visitor's.
    NOT independently verified against the actual production proxy chain —
    confirm which header Fly/Cloudflare actually populate once this is
    live behind the real domain; may need Fly-Client-IP or
    CF-Connecting-IP instead.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
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
    """
    if not check_rate_limit(_client_ip(request)):
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
