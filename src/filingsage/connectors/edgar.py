"""EDGAR connector: discovery of new filings via SEC's submissions API.

Fair-access compliance (SEC policy, non-negotiable):
  * declared User-Agent carrying a real contact email
  * request rate capped well below SEC's 10 req/s ceiling
  * exponential backoff on 403/429/5xx (SEC signals throttling with 403)
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import httpx

from filingsage import __version__
from filingsage.connectors.base import SourceConnector
from filingsage.connectors.models import FilingRef

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"
DEFAULT_FORMS: tuple[str, ...] = ("10-K", "10-Q", "8-K")
RETRYABLE_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


class UnknownTickerError(LookupError):
    """Ticker not present in SEC's company_tickers.json mapping."""


class RateLimiter:
    """Min-interval limiter: guarantees <= max_per_second across sequential calls.

    Hand-rolled (~10 lines) instead of a library: single-process sequential
    polling needs nothing fancier, and every line is explainable. `sleep` is
    injectable so tests run instantly.
    """

    def __init__(self, max_per_second: float, sleep: Callable[[float], None] = time.sleep):
        self._interval = 1.0 / max_per_second
        self._sleep = sleep
        self._next_ok = 0.0

    def wait(self) -> None:
        delay = self._next_ok - time.monotonic()
        if delay > 0:
            self._sleep(delay)
        self._next_ok = max(time.monotonic(), self._next_ok) + self._interval


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
    ):
        if not contact_email or "example.com" in contact_email or contact_email.startswith("change-me"):
            raise ValueError(
                "SEC_CONTACT_EMAIL must be a real contact address before any EDGAR "
                "request is made (SEC fair-access policy). Set it in .env."
            )
        self._limiter = RateLimiter(max_per_second, sleep=sleep)
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
            # a filing with the wrong date.
            rows = zip(
                recent["accessionNumber"],
                recent["form"],
                recent["filingDate"],
                recent["primaryDocument"],
                strict=True,
            )
            for accession, form, filed, primary in rows:
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
                    )
                )
        return found

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