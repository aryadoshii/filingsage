"""SourceConnector — the seam that keeps FilingSage source-agnostic.

Everything downstream (parsing, chunking, agents) consumes FilingRefs and raw
documents; nothing downstream knows EDGAR exists. A future NSE/BSE connector
(roadmap) implements this same interface and the pipeline doesn't change.

The interface grows with the pipeline: discover() today, fetch_raw() lands
with the Week 1 fetch/parse work. We add methods when the pipeline needs
them, not speculatively.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date

from filingsage.connectors.models import FilingRef


class SourceConnector(ABC):
    """One implementation per data source (EDGAR first)."""

    name: str

    @abstractmethod
    def discover(
        self,
        watchlist: Sequence[str],
        *,
        forms: Sequence[str] | None = None,
        since: date | None = None,
    ) -> list[FilingRef]:
        """Return filings for the watched tickers, filtered by form and date.

        Must be read-only and idempotent: calling it twice discovers the same
        filings twice. Deduplication against already-seen accession numbers is
        the pipeline's job (Postgres `filings` table, Week 1), not the
        connector's.
        """
