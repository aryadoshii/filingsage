"""Read-only endpoints for the dashboard: companies, filings, pipeline stats,
recent events, citation lookups, and company research pages (financials,
key stats, 8-K events).

Thin by design — each route opens a session, calls one function from
db/queries.py, and shapes the result. No auth yet, same as /qa (real JWT
auth is roadmap L7); these only read public SEC data the pipeline has
already ingested, and every list is capped so a request can't ask for an
unbounded result.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from filingsage.db import queries
from filingsage.db.session import session_scope
from filingsage.financials import report

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
    last_ingest_at: datetime | None       # latest ingest.completed: "last checked EDGAR"
    last_reconcile_at: datetime | None    # latest pipeline.reconciled
    needs_attention: int                  # stuck filings that failed 3+ times
    quarantined: int


class AttentionOut(BaseModel):
    ticker: str
    company: str
    form_type: str
    filed_at: date
    accession_no: str
    status: str
    failures: int
    attempts: int
    last_error: str | None
    last_failed_at: datetime
    edgar_url: str


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


@router.get("/filings/needs-attention")
def get_needs_attention(
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
) -> list[AttentionOut]:
    """Filings the pipeline has given up retrying (decision #37), with the
    error from their latest failure — what someone has to look at."""
    with session_scope() as session:
        return [AttentionOut(**asdict(row)) for row in queries.needs_attention(session, limit=limit)]


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


# --- Company research pages ---------------------------------------------------


class KeyStatOut(BaseModel):
    key: str
    value: float | None
    as_of: date | None


class CompanyDetailOut(BaseModel):
    ticker: str
    name: str
    cik: int
    sector: str | None
    fiscal_year_end: str | None
    exchange: str | None
    filings: int
    embedded: int
    latest_filed_at: date | None
    financials_updated_at: datetime | None
    key_stats: list[KeyStatOut]


class StatementColumnOut(BaseModel):
    start: date | None
    end: date
    values: dict[str, float | None]
    derived: list[str]


class StatementOut(BaseModel):
    period: Literal["quarter", "annual"]
    columns: list[StatementColumnOut]


class CompanyEventOut(BaseModel):
    filed_at: date
    accession_no: str
    form_type: str
    item_codes: list[str]
    events: list[str]
    notable: bool
    status: str
    edgar_url: str


class OverviewRowOut(BaseModel):
    ticker: str
    name: str
    sector: str | None
    filings: int
    embedded: int
    latest_filed_at: date | None
    revenue_ttm: float | None
    revenue_growth_yoy: float | None
    net_margin_ttm: float | None
    as_of: date | None


def _detail_or_404(session, ticker: str) -> queries.CompanyDetail:
    detail = queries.company_detail(session, ticker)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"{ticker.upper()} isn't tracked yet. Add it from the Filings page.",
        )
    return detail


@router.get("/overview")
def get_overview() -> list[OverviewRowOut]:
    """Every tracked company with its headline numbers — the market-overview
    grid. One facts query for all companies, not one per company."""
    with session_scope() as session:
        companies = queries.list_companies(session)
        facts = queries.facts_by_cik(session)
        sectors = queries.sectors_by_cik(session)
        rows = []
        for c in companies:
            stats = {s.key: s for s in report.key_stats(facts.get(c.cik, []))}
            rows.append(OverviewRowOut(
                ticker=c.ticker, name=c.name, sector=sectors.get(c.cik),
                filings=c.filings, embedded=c.embedded, latest_filed_at=c.latest_filed_at,
                revenue_ttm=stats["revenue_ttm"].value,
                revenue_growth_yoy=stats["revenue_growth_yoy"].value,
                net_margin_ttm=stats["net_margin_ttm"].value,
                as_of=stats["revenue_ttm"].as_of,
            ))
        return rows


@router.get("/companies/{ticker}")
def get_company(ticker: str) -> CompanyDetailOut:
    with session_scope() as session:
        detail = _detail_or_404(session, ticker)
        stats = report.key_stats(queries.company_facts(session, detail.cik))
        return CompanyDetailOut(
            **asdict(detail),
            key_stats=[KeyStatOut(key=s.key, value=s.value, as_of=s.as_of) for s in stats],
        )


@router.get("/companies/{ticker}/financials")
def get_financials(
    ticker: str,
    period: Literal["quarter", "annual"] = "quarter",
    limit: int = Query(default=8, ge=1, le=20),
) -> StatementOut:
    """Income statement, cash flow and balance-sheet lines plus margins and
    growth, one column per period, oldest first. `derived` lists the values
    in a column that were computed (e.g. Q4 = full year minus Q1-Q3)."""
    with session_scope() as session:
        detail = _detail_or_404(session, ticker)
        columns = report.statement(
            queries.company_facts(session, detail.cik), period=period, limit=limit
        )
        return StatementOut(
            period=period,
            columns=[
                StatementColumnOut(start=c.start, end=c.end, values=c.values, derived=c.derived)
                for c in columns
            ],
        )


@router.get("/companies/{ticker}/events")
def get_events_for_company(
    ticker: str, limit: int = Query(default=30, ge=1, le=MAX_LIST_LIMIT)
) -> list[CompanyEventOut]:
    with session_scope() as session:
        detail = _detail_or_404(session, ticker)
        return [
            CompanyEventOut(**asdict(e))
            for e in queries.company_events(session, detail.cik, limit=limit)
        ]
