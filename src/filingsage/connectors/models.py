"""Shared connector data types — source-agnostic by design."""

from datetime import date

from pydantic import BaseModel, ConfigDict


class FilingRef(BaseModel):
    """A discovered filing: enough identity to fetch it later, nothing more.

    frozen=True makes instances immutable and hashable, so refs can live in
    sets and be deduplicated safely.
    """

    model_config = ConfigDict(frozen=True)

    cik: int
    ticker: str
    company: str
    accession_number: str  # e.g. "0000320193-25-000073" — EDGAR's primary key
    form_type: str         # e.g. "10-K"
    filed_at: date
    primary_document: str  # e.g. "aapl-20250628.htm"
    items: str = ""        # 8-K item codes, e.g. "2.02,9.01"; empty for 10-K/10-Q


class CompanyProfile(BaseModel):
    """Company details from EDGAR's submissions API, plus the 8-K item codes
    of every filing in its recent window (used to backfill filings that were
    discovered before item codes were stored)."""

    model_config = ConfigDict(frozen=True)

    cik: int
    name: str
    sector: str | None        # SIC description, e.g. "Electronic Computers"
    fiscal_year_end: str | None  # MMDD, e.g. "0927"
    exchange: str | None
    items_by_accession: dict[str, str]
