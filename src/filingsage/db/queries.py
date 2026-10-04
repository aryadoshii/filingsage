"""Read-side queries for the dashboard and API: companies, filings, pipeline
stats, recent events, and citation lookups.

Library-level on purpose (same convention as gold/qa.py's
resolve_citations): the API routes in api/catalog.py are thin wrappers, and
tests reach these functions directly with a real Postgres. Every function
takes an open Session and only reads — none of them commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from filingsage.connectors import edgar_items
from filingsage.db.models import Chunk, Company, Event, Filing, FilingStatus, FinancialFact

EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


def edgar_document_url(cik: int, accession_no: str, primary_document: str) -> str:
    """The filing's primary document on sec.gov — what a citation links to.

    EDGAR's archive path uses the CIK without zero-padding and the accession
    number without dashes (0000320193-25-000079 -> 000032019325000079).
    """
    return f"{EDGAR_ARCHIVES}/{cik}/{accession_no.replace('-', '')}/{primary_document}"


@dataclass(frozen=True, slots=True)
class CompanyRow:
    ticker: str
    name: str
    cik: int
    filings: int
    embedded: int
    latest_filed_at: date | None


def list_companies(session: Session) -> list[CompanyRow]:
    embedded = func.count(case((Filing.status == FilingStatus.EMBEDDED.value, 1)))
    rows = session.execute(
        select(
            Company.ticker,
            Company.name,
            Company.cik,
            func.count(Filing.id).label("filings"),
            embedded.label("embedded"),
            func.max(Filing.filed_at).label("latest_filed_at"),
        )
        .outerjoin(Filing, Filing.cik == Company.cik)
        .group_by(Company.cik)
        .order_by(Company.ticker)
    ).all()
    return [CompanyRow(**row._mapping) for row in rows]


@dataclass(frozen=True, slots=True)
class FilingRow:
    id: int
    ticker: str
    company: str
    form_type: str
    filed_at: date
    accession_no: str
    status: str
    chunk_count: int
    edgar_url: str


def list_filings(
    session: Session,
    *,
    ticker: str | None = None,
    form_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[FilingRow]:
    """Newest filings first, optionally filtered. chunk_count comes from a
    grouped subquery so a filing with no chunks yet reports 0, not a
    missing row."""
    chunk_counts = (
        select(Chunk.filing_id, func.count(Chunk.id).label("n"))
        .group_by(Chunk.filing_id)
        .subquery()
    )
    query = (
        select(
            Filing.id,
            Company.ticker,
            Company.name.label("company"),
            Filing.form_type,
            Filing.filed_at,
            Filing.accession_no,
            Filing.status,
            Filing.cik,
            Filing.primary_document,
            func.coalesce(chunk_counts.c.n, 0).label("chunk_count"),
        )
        .join(Company, Company.cik == Filing.cik)
        .outerjoin(chunk_counts, chunk_counts.c.filing_id == Filing.id)
        .order_by(Filing.filed_at.desc(), Filing.id.desc())
        .limit(limit)
    )
    if ticker:
        query = query.where(Company.ticker == ticker.upper())
    if form_type:
        query = query.where(Filing.form_type == form_type)
    if status:
        query = query.where(Filing.status == status)

    return [
        FilingRow(
            id=row.id,
            ticker=row.ticker,
            company=row.company,
            form_type=row.form_type,
            filed_at=row.filed_at,
            accession_no=row.accession_no,
            status=row.status,
            chunk_count=row.chunk_count,
            edgar_url=edgar_document_url(row.cik, row.accession_no, row.primary_document),
        )
        for row in session.execute(query).all()
    ]


@dataclass(frozen=True, slots=True)
class PipelineStats:
    filings_by_status: dict[str, int]
    total_filings: int
    companies: int
    chunks: int
    embedded_chunks: int
    last_event_at: datetime | None


def pipeline_stats(session: Session) -> PipelineStats:
    by_status = dict(
        session.execute(select(Filing.status, func.count()).group_by(Filing.status)).all()
    )
    chunks, embedded_chunks = session.execute(
        select(func.count(Chunk.id), func.count(Chunk.qdrant_point_id))
    ).one()
    return PipelineStats(
        filings_by_status=by_status,
        total_filings=sum(by_status.values()),
        companies=session.scalar(select(func.count()).select_from(Company)) or 0,
        chunks=chunks,
        embedded_chunks=embedded_chunks,
        last_event_at=session.scalar(select(func.max(Event.created_at))),
    )


@dataclass(frozen=True, slots=True)
class EventRow:
    type: str
    entity_id: str
    payload: dict
    created_at: datetime


def recent_events(session: Session, *, limit: int = 25) -> list[EventRow]:
    rows = session.scalars(
        select(Event).order_by(Event.created_at.desc(), Event.id.desc()).limit(limit)
    ).all()
    return [
        EventRow(type=e.type, entity_id=e.entity_id, payload=e.payload_json, created_at=e.created_at)
        for e in rows
    ]


@dataclass(frozen=True, slots=True)
class CitationRow:
    chunk_id: int
    ticker: str
    company: str
    form_type: str
    filed_at: date
    section: str
    accession_no: str
    text: str
    edgar_url: str


def get_citations(session: Session, chunk_ids: list[int]) -> list[CitationRow]:
    """Everything needed to display a cited chunk: where it came from, the
    exact text the model was shown, and a link to the filing on sec.gov.
    Unknown ids are simply absent from the result (same contract as
    gold/qa.py's resolve_citations)."""
    if not chunk_ids:
        return []
    rows = session.execute(
        select(
            Chunk.id,
            Chunk.section,
            Chunk.text,
            Filing.accession_no,
            Filing.form_type,
            Filing.filed_at,
            Filing.cik,
            Filing.primary_document,
            Company.ticker,
            Company.name,
        )
        .join(Filing, Filing.id == Chunk.filing_id)
        .join(Company, Company.cik == Filing.cik)
        .where(Chunk.id.in_(chunk_ids))
        .order_by(Chunk.id)
    ).all()
    return [
        CitationRow(
            chunk_id=row.id,
            ticker=row.ticker,
            company=row.name,
            form_type=row.form_type,
            filed_at=row.filed_at,
            section=row.section,
            accession_no=row.accession_no,
            text=row.text,
            edgar_url=edgar_document_url(row.cik, row.accession_no, row.primary_document),
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class CompanyDetail:
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


def company_detail(session: Session, ticker: str) -> CompanyDetail | None:
    company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        return None
    filings, embedded, latest = session.execute(
        select(
            func.count(Filing.id),
            func.count(case((Filing.status == FilingStatus.EMBEDDED.value, 1))),
            func.max(Filing.filed_at),
        ).where(Filing.cik == company.cik)
    ).one()
    return CompanyDetail(
        ticker=company.ticker, name=company.name, cik=company.cik, sector=company.sector,
        fiscal_year_end=company.fiscal_year_end, exchange=company.exchange,
        filings=filings, embedded=embedded, latest_filed_at=latest,
        financials_updated_at=company.financials_updated_at,
    )


def company_facts(session: Session, cik: int) -> list[FinancialFact]:
    return list(session.scalars(select(FinancialFact).where(FinancialFact.cik == cik)))


def facts_by_cik(session: Session) -> dict[int, list[FinancialFact]]:
    grouped: dict[int, list[FinancialFact]] = {}
    for fact in session.scalars(select(FinancialFact)):
        grouped.setdefault(fact.cik, []).append(fact)
    return grouped


@dataclass(frozen=True, slots=True)
class EventRowOut:
    filed_at: date
    accession_no: str
    form_type: str
    item_codes: list[str]
    events: list[str]
    notable: bool
    status: str
    edgar_url: str


def company_events(session: Session, cik: int, *, limit: int = 30) -> list[EventRowOut]:
    """The company's 8-Ks, newest first, as plain-language events."""
    rows = session.scalars(
        select(Filing)
        .where(Filing.cik == cik, Filing.form_type.in_(("8-K", "8-K/A")))
        .order_by(Filing.filed_at.desc(), Filing.id.desc())
        .limit(limit)
    ).all()
    out = []
    for f in rows:
        codes = edgar_items.parse_items(f.items)
        infos = edgar_items.describe(codes) if codes else []
        out.append(
            EventRowOut(
                filed_at=f.filed_at,
                accession_no=f.accession_no,
                form_type=f.form_type,
                item_codes=codes,
                events=[i.label for i in infos] or ["Current report"],
                notable=any(i.notable for i in infos),
                status=f.status,
                edgar_url=edgar_document_url(f.cik, f.accession_no, f.primary_document),
            )
        )
    return out


def sectors_by_cik(session: Session) -> dict[int, str | None]:
    return dict(session.execute(select(Company.cik, Company.sector)).all())
