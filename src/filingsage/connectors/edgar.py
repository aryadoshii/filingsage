"""EDGAR connector: discovery of new filings via SEC's submissions API.

Fair-access compliance (SEC policy, non-negotiable):
  * declared User-Agent carrying a real contact email
  * request rate capped well below SEC's 10 req/s ceiling — across every
    process at once when a shared limiter is injected (connectors/rate_limit.py)
  * exponential backoff on 403/429/5xx (SEC signals throttling with 403)
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Protocol

import httpx

from filingsage import __version__
from filingsage.connectors.base import SourceConnector
from filingsage.connectors.models import CompanyProfile, FilingRef

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# Every XBRL fact a company has ever reported, in one JSON document — the
# official source behind financial statements on any finance site.
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"
DEFAULT_FORMS: tuple[str, ...] = ("10-K", "10-Q", "8-K")
RETRYABLE_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


class UnknownTickerError(LookupError):
    """Ticker not present in SEC's company_tickers.json mapping."""


class Limiter(Protocol):
    """Anything EdgarClient can call before each request: block until one
    more request is allowed."""

    def wait(self) -> None: ...


class RateLimiter:
    """Min-interval limiter: guarantees <= max_per_second across sequential calls.

    Hand-rolled (~10 lines) instead of a library: single-process sequential
    polling needs nothing fancier, and every line is explainable. `sleep`
    and `clock` are injectable so tests run instantly.

    Per-process only — two processes each holding one can together exceed
    the rate. The pipeline uses connectors/rate_limit.py's shared limiter,
    which falls back to this one when Redis is unreachable.
    """

    def __init__(
        self,
        max_per_second: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._interval = 1.0 / max_per_second
        self._sleep = sleep
        self._clock = clock
        self._next_ok = 0.0

    def wait(self) -> None:
        delay = self._next_ok - self._clock()
        if delay > 0:
            self._sleep(delay)
        self._next_ok = max(self._clock(), self._next_ok) + self._interval


class EdgarClient:
    """Thin HTTP wrapper that makes SEC fair-access impossible to forget.

    Every EDGAR request in the codebase goes through this class, so the
    User-Agent, rate cap, and backoff are enforced in exactly one place.
    """

    def __init__(
        self,
        contact_email: str,
        max_per_second: float = 8.0,  # deliberate headroom under SEC's 10/s
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,  # test seam
        sleep: Callable[[float], None] = time.sleep,   # test seam
        limiter: Limiter | None = None,  # e.g. the cross-process shared limiter
    ):
        if not contact_email or "example.com" in contact_email or contact_email.startswith("change-me"):
            raise ValueError(
                "SEC_CONTACT_EMAIL must be a real contact address before any EDGAR "
                "request is made (SEC fair-access policy). Set it in .env."
            )
        # Without an injected limiter, an in-process one at max_per_second —
        # right for a single process (tests, one-off scripts), not for the
        # pipeline, whose processes must share one budget.
        self._limiter = limiter or RateLimiter(max_per_second, sleep=sleep)
        self._sleep = sleep
        self._max_retries = max_retries
        self._client = httpx.Client(
            headers={
                "User-Agent": f"FilingSage/{__version__} {contact_email}",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=30.0,
            transport=transport,
        )

    def _request(self, url: str) -> httpx.Response:
        backoff = 1.0
        for attempt in range(1, self._max_retries + 1):
            self._limiter.wait()
            resp = self._client.get(url)
            if resp.status_code == 200:
                return resp
            if resp.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.replace(".", "", 1).isdigit():
                    delay = float(retry_after)  # server knows best — honor it
                else:
                    delay = backoff + random.uniform(0, 0.5)  # jitter avoids sync'd retries
                self._sleep(delay)
                backoff = min(backoff * 2.0, 60.0)
                continue
            resp.raise_for_status()
        raise RuntimeError("unreachable: retry loop exits via return or raise")

    def get_json(self, url: str) -> dict:
        return self._request(url).json()

    def get_bytes(self, url: str) -> bytes:
        return self._request(url).content


class EdgarConnector(SourceConnector):
    name = "edgar"

    def __init__(self, client: EdgarClient, bronze_dir: Path):
        self._client = client
        self._bronze = bronze_dir
        self._ticker_map: dict[str, dict] | None = None

    def _load_ticker_map(self) -> dict[str, dict]:
        """Fetch SEC's ticker->CIK mapping once per connector instance."""
        if self._ticker_map is None:
            raw = self._client.get_json(TICKER_MAP_URL)
            self._write_bronze(Path("reference") / "company_tickers.json", raw)
            self._ticker_map = {row["ticker"].upper(): row for row in raw.values()}
        return self._ticker_map

    def resolve(self, ticker: str) -> tuple[int, str]:
        row = self._load_ticker_map().get(ticker.upper())
        if row is None:
            raise UnknownTickerError(f"{ticker!r} not found in SEC company_tickers.json")
        return int(row["cik_str"]), row["title"]

    def discover(
        self,
        watchlist: Sequence[str],
        *,
        forms: Sequence[str] | None = None,
        since: date | None = None,
    ) -> list[FilingRef]:
        wanted = frozenset(forms or DEFAULT_FORMS)
        found: list[FilingRef] = []
        for ticker in watchlist:
            cik, company = self.resolve(ticker)
            data = self._client.get_json(SUBMISSIONS_URL.format(cik=cik))
            self._write_bronze(Path("submissions") / f"CIK{cik:010d}.json", data)

            recent = data["filings"]["recent"]
            # EDGAR returns parallel arrays, not a list of objects. strict=True
            # makes a length mismatch fail loudly instead of silently pairing
            # a filing with the wrong date. "items" (8-K item codes) is
            # treated as optional so a response without it still parses.
            items = recent.get("items") or [""] * len(recent["accessionNumber"])
            rows = zip(
                recent["accessionNumber"],
                recent["form"],
                recent["filingDate"],
                recent["primaryDocument"],
                items,
                strict=True,
            )
            for accession, form, filed, primary, item_codes in rows:
                if form not in wanted:
                    continue
                filed_at = date.fromisoformat(filed)
                if since is not None and filed_at < since:
                    continue
                found.append(
                    FilingRef(
                        cik=cik,
                        ticker=ticker.upper(),
                        company=company,
                        accession_number=accession,
                        form_type=form,
                        filed_at=filed_at,
                        primary_document=primary,
                        items=item_codes or "",
                    )
                )
        return found

    def profile(self, cik: int) -> CompanyProfile:
        """Company details + 8-K item codes from the submissions API."""
        data = self._client.get_json(SUBMISSIONS_URL.format(cik=cik))
        self._write_bronze(Path("submissions") / f"CIK{cik:010d}.json", data)
        return parse_profile(cik, data)

    def company_facts(self, cik: int) -> dict | None:
        """The company's full XBRL fact set, or None if it has none.

        A 404 is a real answer here, not an error: companies that have never
        filed XBRL financial statements (rare among listed US companies)
        simply have no companyfacts document.
        """
        try:
            data = self._client.get_json(COMPANY_FACTS_URL.format(cik=cik))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        self._write_bronze(Path("xbrl") / f"CIK{cik:010d}.json", data)
        return data

    def bronze_path(self, ref: FilingRef) -> Path:
        """Immutable bronze location: keyed by accession number (spec §5)."""
        return self._bronze / "filings" / ref.accession_number / ref.primary_document

    def fetch_raw(self, ref: FilingRef) -> Path:
        """Fetch the primary document into immutable, accession-keyed bronze.

        Idempotent by design, and the existence check runs BEFORE any network
        call: bronze is immutable and the accession number is EDGAR's global
        primary key, so a re-fetch can never produce different bytes worth
        having — skipping saves rate-limit budget.

        The write is atomic (tmp file + rename): a crash mid-write can never
        leave a truncated document that a later run mistakes for real bronze.
        """
        dest = self.bronze_path(ref)
        if dest.exists():
            return dest
        url = ARCHIVES_URL.format(
            cik=ref.cik,
            accession_nodash=ref.accession_number.replace("-", ""),
            document=ref.primary_document,
        )
        payload = self._client.get_bytes(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(dest)  # atomic on POSIX
        return dest

    def _write_bronze(self, rel: Path, payload: dict) -> Path:
        """Snapshot raw API responses to bronze.

        Submissions snapshots are polling state (latest wins). The immutable,
        accession-keyed bronze starts with document fetch in Week 1.
        """
        path = self._bronze / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path


def parse_profile(cik: int, data: dict) -> CompanyProfile:
    """Pure: submissions JSON -> CompanyProfile. Separate from profile() so
    tests can feed it a fixture without any HTTP."""
    recent = data.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    items = recent.get("items") or [""] * len(accessions)
    exchanges = [x for x in (data.get("exchanges") or []) if x]
    return CompanyProfile(
        cik=cik,
        name=data.get("name") or "",
        sector=data.get("sicDescription") or None,
        fiscal_year_end=data.get("fiscalYearEnd") or None,
        exchange=exchanges[0] if exchanges else None,
        items_by_accession={acc: code for acc, code in zip(accessions, items, strict=False) if code},
    )
