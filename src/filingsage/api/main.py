"""FastAPI application entrypoint."""

from fastapi import FastAPI

from filingsage import __version__
from filingsage.config import get_settings

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
