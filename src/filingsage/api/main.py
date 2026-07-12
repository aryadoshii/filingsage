"""FastAPI application entrypoint."""

import secrets

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from filingsage import __version__
from filingsage.config import get_settings
from filingsage.worker.tasks import ingest_watchlist

app = FastAPI(
    title="FilingSage",
    version=__version__,
    description=(
        "An AI research analyst that watches the companies you care about, "
        "reads every new SEC filing the moment it drops, and answers with citations."
    ),
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
