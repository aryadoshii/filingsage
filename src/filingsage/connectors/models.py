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
