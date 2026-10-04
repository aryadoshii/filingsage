"""Read-only endpoints for the dashboard: companies, filings, pipeline stats,
recent events, and citation lookups.

Thin by design — each route opens a session, calls one function from
db/queries.py, and shapes the result. No auth yet, same as /qa (real JWT
auth is roadmap L7); these only read public SEC data the pipeline has
already ingested, and every list is capped so a request can't ask for an
unbounded result.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from filingsage.db import queries
from filingsage.db.session import session_scope

router = APIRouter(tags=["catalog"])

# Hard cap on any list endpoint, and on how many chunk ids one citation
# lookup may ask for (an answer cites at most LLM_CONTEXT_TOP_N chunks).
MAX_LIST_LIMIT = 200
MAX_CITATION_IDS = 50


class CompanyOut(BaseModel):
    ticker: str
    name: str
    cik: int
    filings: int
    embedded: int
    latest_filed_at: date | None


class FilingOut(BaseModel):
    id: int
    ticker: str
    company: str
    form_type: str
    filed_at: date
    accession_no: str
    status: str
    chunk_count: int
    edgar_url: str


class StatsOut(BaseModel):
    filings_by_status: dict[str, int]
    total_filings: int
    companies: int
    chunks: int
    embedded_chunks: int
    last_event_at: datetime | None


class EventOut(BaseModel):
    type: str
    entity_id: str
    payload: dict
    created_at: datetime


class CitationOut(BaseModel):
    chunk_id: int
    ticker: str
    company: str
    form_type: str
    filed_at: date
    section: str
    accession_no: str
    text: str
    edgar_url: str


@router.get("/companies")
def get_companies() -> list[CompanyOut]:
    with session_scope() as session:
        return [CompanyOut(**asdict(row)) for row in queries.list_companies(session)]


@router.get("/filings")
def get_filings(
    ticker: str | None = None,
    form_type: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
) -> list[FilingOut]:
    with session_scope() as session:
        rows = queries.list_filings(
            session, ticker=ticker, form_type=form_type, status=status, limit=limit
        )
        return [FilingOut(**asdict(row)) for row in rows]


@router.get("/stats")
def get_stats() -> StatsOut:
    with session_scope() as session:
        return StatsOut(**asdict(queries.pipeline_stats(session)))


@router.get("/events")
def get_events(limit: int = Query(default=25, ge=1, le=MAX_LIST_LIMIT)) -> list[EventOut]:
    with session_scope() as session:
        return [EventOut(**asdict(row)) for row in queries.recent_events(session, limit=limit)]


@router.get("/citations")
def get_citations(ids: str = Query(description="Comma-separated chunk ids, e.g. 12,40,41")) -> list[CitationOut]:
    """Resolve the chunk ids an answer cites into displayable sources: the
    exact excerpt the model saw, plus filing metadata and a sec.gov link."""
    try:
        chunk_ids = sorted({int(part) for part in ids.split(",") if part.strip()})
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers") from None
    if len(chunk_ids) > MAX_CITATION_IDS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_CITATION_IDS} ids per request")
    with session_scope() as session:
        return [CitationOut(**asdict(row)) for row in queries.get_citations(session, chunk_ids)]
