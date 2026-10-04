"""Thin HTTP client for the FilingSage API. No Streamlit imports here, so it
can be tested with httpx.MockTransport and reused by any Python caller.

Every failure becomes an ApiError carrying a message written for the person
using the dashboard — what went wrong and what to do — never a raw stack
trace or status line.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Inside Compose this is http://api:8000 (set in docker-compose.yml); a
# dashboard started on the host falls back to the published port.
DEFAULT_API_URL = "http://localhost:8000"

# Q&A runs retrieval, a cross-encoder rerank and an LLM call (with a
# fallback provider) — far slower than a read. Reads should fail fast so a
# stopped API shows up immediately instead of hanging the page.
READ_TIMEOUT = httpx.Timeout(10.0)
ASK_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


class ApiError(Exception):
    """A failed API call, with a message safe to show the user."""


class FilingSageClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        ingest_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = (base_url or os.environ.get("API_URL") or DEFAULT_API_URL).rstrip("/")
        self.ingest_token = ingest_token if ingest_token is not None else os.environ.get(
            "INGEST_TOKEN", ""
        )
        self._transport = transport

    # -- plumbing -----------------------------------------------------------

    def _request(
        self, method: str, path: str, *, timeout: httpx.Timeout = READ_TIMEOUT, **kwargs: Any
    ) -> Any:
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=timeout, transport=self._transport
            ) as client:
                resp = client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ApiError(
                "The API took too long to respond. It may still be loading its models — "
                "try again in a moment."
            ) from None
        except httpx.TransportError:
            raise ApiError(
                f"Can't reach the API at {self.base_url}. Check that the stack is running "
                "with `docker compose ps`."
            ) from None

        if resp.status_code == 429:
            raise ApiError("Too many questions in a short time. Wait a minute and ask again.")
        if resp.status_code == 503:
            detail = _detail(resp) or "The service is temporarily unavailable."
            raise ApiError(f"{detail} Check `docker compose logs api` for the cause.")
        if resp.status_code >= 400:
            raise ApiError(_detail(resp) or f"The API rejected the request ({resp.status_code}).")
        return resp.json()

    # -- reads --------------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/healthz")

    def stats(self) -> dict:
        return self._request("GET", "/stats")

    def companies(self) -> list[dict]:
        return self._request("GET", "/companies")

    def filings(
        self,
        *,
        ticker: str | None = None,
        form_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        params = {"ticker": ticker, "form_type": form_type, "status": status, "limit": limit}
        return self._request(
            "GET", "/filings", params={k: v for k, v in params.items() if v is not None}
        )

    def events(self, *, limit: int = 25) -> list[dict]:
        return self._request("GET", "/events", params={"limit": limit})

    def citations(self, chunk_ids: list[int]) -> list[dict]:
        if not chunk_ids:
            return []
        return self._request(
            "GET", "/citations", params={"ids": ",".join(str(i) for i in chunk_ids)}
        )

    # -- actions ------------------------------------------------------------

    def ask(
        self,
        question: str,
        *,
        ticker: str | None = None,
        form_type: str | None = None,
        since: str | None = None,
    ) -> dict:
        body = {"question": question, "ticker": ticker, "form_type": form_type, "since": since}
        return self._request(
            "POST", "/qa", json={k: v for k, v in body.items() if v is not None},
            timeout=ASK_TIMEOUT,
        )

    def track(self, tickers: list[str], *, limit: int) -> str:
        """Start ingestion for `tickers`; returns the queued task id.

        Runs server-side inside the Streamlit process, so INGEST_TOKEN never
        reaches the browser.
        """
        if not self.ingest_token:
            raise ApiError(
                "Adding companies needs INGEST_TOKEN set in .env (the same value the API "
                "uses). See the README's 'Dashboard' section."
            )
        result = self._request(
            "POST", "/internal/ingest",
            json={"tickers": tickers, "limit": limit},
            headers={"X-Ingest-Token": self.ingest_token},
        )
        return result["task_id"]


def _detail(resp: httpx.Response) -> str | None:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:  # FastAPI validation errors
        return "; ".join(str(item.get("msg", item)) for item in detail)
    return None
