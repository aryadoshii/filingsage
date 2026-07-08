"""EDGAR connector tests.

All HTTP is faked with httpx.MockTransport (ships with httpx — no new dep,
no network). sleep is injected so retry/rate-limit tests run instantly.
"""

from datetime import date

import httpx
import pytest

from filingsage.connectors.edgar import (
    EdgarClient,
    EdgarConnector,
    RateLimiter,
    UnknownTickerError,
)

TICKER_FIXTURE = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}

SUBMISSIONS_FIXTURE = {
    "cik": "320193",
    "filings": {
        "recent": {
            "accessionNumber": ["acc-10k", "acc-form4", "acc-8k", "acc-10q"],
            "form": ["10-K", "4", "8-K", "10-Q"],
            "filingDate": ["2026-06-30", "2026-06-01", "2026-05-15", "2026-02-10"],
            "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm"],
        }
    },
}


class Recorder:
    """MockTransport handler that records requests; can fail first submissions call."""

    def __init__(self, submissions_429_first: bool = False):
        self.requests: list[httpx.Request] = []
        self._to_fail = 1 if submissions_429_first else 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "company_tickers" in url:
            return httpx.Response(200, json=TICKER_FIXTURE)
        if "/submissions/" in url:
            if self._to_fail > 0:
                self._to_fail -= 1
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json=SUBMISSIONS_FIXTURE)
        return httpx.Response(404)

    def count(self, fragment: str) -> int:
        return sum(fragment in str(r.url) for r in self.requests)


def make_connector(tmp_path, handler: Recorder | None = None):
    handler = handler or Recorder()
    sleeps: list[float] = []
    client = EdgarClient(
        contact_email="arya@test.dev",
        max_per_second=10_000,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return EdgarConnector(client, bronze_dir=tmp_path / "bronze"), handler, sleeps


def test_rejects_placeholder_contact_email():
    with pytest.raises(ValueError, match="SEC_CONTACT_EMAIL"):
        EdgarClient(contact_email="change-me@example.com")


def test_declared_user_agent_on_every_request(tmp_path):
    connector, handler, _ = make_connector(tmp_path)
    connector.discover(["AAPL"])
    assert handler.requests, "expected at least one request"
    for req in handler.requests:
        ua = req.headers["User-Agent"]
        assert "FilingSage" in ua and "arya@test.dev" in ua


def test_discover_filters_to_watched_forms(tmp_path):
    connector, _, _ = make_connector(tmp_path)
    refs = connector.discover(["AAPL"])
    assert {r.form_type for r in refs} == {"10-K", "8-K", "10-Q"}
    assert len(refs) == 3
    assert all(r.ticker == "AAPL" and r.cik == 320193 for r in refs)


def test_discover_since_filters_by_date(tmp_path):
    connector, _, _ = make_connector(tmp_path)
    refs = connector.discover(["AAPL"], since=date(2026, 6, 1))
    assert [r.accession_number for r in refs] == ["acc-10k"]


def test_unknown_ticker_raises(tmp_path):
    connector, _, _ = make_connector(tmp_path)
    with pytest.raises(UnknownTickerError, match="ZZZZTOP"):
        connector.discover(["ZZZZTOP"])


def test_ticker_map_fetched_once_for_many_tickers(tmp_path):
    connector, handler, _ = make_connector(tmp_path)
    connector.discover(["AAPL", "MSFT"])
    assert handler.count("company_tickers") == 1
    assert handler.count("/submissions/") == 2


def test_retries_on_429_then_succeeds(tmp_path):
    connector, handler, sleeps = make_connector(tmp_path, Recorder(submissions_429_first=True))
    refs = connector.discover(["AAPL"])
    assert len(refs) == 3
    assert handler.count("/submissions/") == 2
    assert 0.0 in sleeps


def test_bronze_snapshots_written(tmp_path):
    connector, _, _ = make_connector(tmp_path)
    connector.discover(["AAPL"])
    bronze = tmp_path / "bronze"
    assert (bronze / "reference" / "company_tickers.json").exists()
    assert (bronze / "submissions" / "CIK0000320193.json").exists()


def test_rate_limiter_spaces_calls():
    sleeps: list[float] = []
    limiter = RateLimiter(max_per_second=2.0, sleep=sleeps.append)
    limiter.wait()
    limiter.wait()
    limiter.wait()
    assert len(sleeps) == 2
    assert 0.4 <= sleeps[0] <= 0.5
    assert sleeps[1] > sleeps[0]