"""Persisting a company's profile and XBRL facts. Both functions join the
caller's open transaction and never commit (same convention as
db/events.emit_event), so a refresh lands atomically or not at all."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from filingsage.connectors.models import CompanyProfile
from filingsage.db.models import Company, Filing, FinancialFact
from filingsage.financials.xbrl import Fact


def apply_profile(session: Session, company: Company, profile: CompanyProfile) -> int:
    """Update company details and backfill 8-K item codes onto filings that
    were discovered before item codes were stored. Returns how many filings
    got item codes."""
    company.sector = profile.sector or company.sector
    company.fiscal_year_end = profile.fiscal_year_end or company.fiscal_year_end
    company.exchange = profile.exchange or company.exchange
    if not profile.items_by_accession:
        return 0
    missing = session.scalars(
        select(Filing).where(
            Filing.cik == company.cik,
            Filing.items.is_(None),
            Filing.accession_no.in_(profile.items_by_accession),
        )
    ).all()
    for filing in missing:
        filing.items = profile.items_by_accession[filing.accession_no]
    return len(missing)


def replace_facts(session: Session, cik: int, facts: Iterable[Fact]) -> int:
    """Swap a company's facts for a freshly extracted set.

    Delete-then-insert rather than upsert: the extraction is a pure function
    of the SEC's current document, so the new set fully supersedes the old
    one — including periods a restatement removed — and re-running is
    idempotent by construction.
    """
    session.execute(delete(FinancialFact).where(FinancialFact.cik == cik))
    rows = [
        FinancialFact(
            cik=cik, metric=f.metric, period=f.period, start=f.start, end=f.end,
            value=f.value, unit=f.unit, derived=f.derived, concept=f.concept,
            accession_no=f.accession_no, filed=f.filed,
        )
        for f in facts
    ]
    session.add_all(rows)
    return len(rows)
