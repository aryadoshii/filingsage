"""Dashboard (ui/) — the framework-free parts: display helpers and the HTTP
client. No Streamlit and no running API: the client is exercised against
httpx.MockTransport, so every error path is checked without a network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError, FilingSageClient

# --- format helpers ----------------------------------------------------------


def test_known_sections_get_reader_friendly_names():
    assert fmt.section_label("risk_factors") == "Risk factors"
    assert fmt.section_label("mdna") == "Management's discussion (MD&A)"


def test_generic_8k_items_and_unknown_keys_still_read_cleanly():
    assert fmt.section_label("item_3.02") == "Item 3.02"
    assert fmt.section_label("something_new") == "Something new"


def test_status_labels_use_plain_language_and_a_tone():
    assert fmt.status_label("embedded") == ("Searchable", "good")
    assert fmt.status_label("quarantined") == ("Couldn't parse", "bad")
    assert fmt.status_label("brand_new_status") == ("Brand_new_status", "pending")


def test_dates_are_unambiguous():
    assert fmt.format_date("2025-10-31") == "31 Oct 2025"
    assert fmt.format_date(None) == "—"


def test_relative_time_buckets():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    assert fmt.relative_time(now - timedelta(seconds=20), now=now) == "just now"
    assert fmt.relative_time(now - timedelta(minutes=5), now=now) == "5 min ago"
    assert fmt.relative_time(now - timedelta(hours=1), now=now) == "1 hour ago"
    assert fmt.relative_time((now - timedelta(days=3)).isoformat(), now=now) == "3 days ago"
    assert fmt.relative_time(None) == "never"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" aapl ", "AAPL"), ("BRK.B", "BRK.B"), ("", None), ("not a ticker", None), ("1ABC", None)],
)
def test_ticker_normalization(raw, expected):
    assert fmt.normalize_ticker(raw) == expected


def test_citation_numbers_follow_first_appearance_and_stay_stable():
    claims = [{"chunk_ids": [40, 12]}, {"chunk_ids": [12, 7]}, {"chunk_ids": [40]}]
    assert fmt.citation_numbers(claims) == {40: 1, 12: 2, 7: 3}


def test_excerpt_is_escaped_and_trimmed_at_a_word():
    out = fmt.excerpt_html("<script>alert(1)</script>  risk " + "word " * 400, max_chars=60)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert out.endswith(" …")
    assert len(out) < 120


def test_event_details_are_human_readable():
    assert fmt.event_detail({"payload": {"chunk_count": 1}}) == "1 passage indexed"
    assert fmt.event_detail({"payload": {"section_count": 4}}) == "4 sections"
    assert fmt.event_detail({"payload": {"ticker": "AAPL", "form_type": "10-K"}}) == "AAPL 10-K"
    assert fmt.event_label("filing.embedded") == "Made searchable"


# --- API client ----------------------------------------------------------------


def _client(handler, token: str = "tok") -> FilingSageClient:
    return FilingSageClient(
        "http://api.test", ingest_token=token, transport=httpx.MockTransport(handler)
    )


def test_ask_sends_only_the_filters_that_are_set():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answer": "ok", "claims": []})

    _client(handler).ask("What changed?", ticker="AAPL")

    assert seen == {"path": "/qa", "body": {"question": "What changed?", "ticker": "AAPL"}}


def test_citations_joins_ids_and_skips_the_call_when_empty():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["ids"])
        return httpx.Response(200, json=[])

    client = _client(handler)
    assert client.citations([]) == []
    client.citations([3, 1])

    assert calls == ["3,1"]


def test_track_sends_the_ingest_token_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("x-ingest-token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"task_id": "t-1"})

    assert _client(handler).track(["NVDA"], limit=10) == "t-1"
    assert seen == {"token": "tok", "body": {"tickers": ["NVDA"], "limit": 10}}


def test_track_without_a_token_explains_how_to_fix_it():
    with pytest.raises(ApiError, match="INGEST_TOKEN"):
        _client(lambda r: httpx.Response(202, json={}), token="").track(["NVDA"], limit=1)


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (429, {"detail": "Rate limit exceeded"}, "Too many questions"),
        (503, {"detail": "Q&A is temporarily unavailable"}, "docker compose logs api"),
        (422, {"detail": [{"msg": "field required"}]}, "field required"),
        (404, {}, "rejected the request"),
    ],
)
def test_http_errors_become_actionable_messages(status, body, message):
    client = _client(lambda r: httpx.Response(status, json=body))
    with pytest.raises(ApiError, match=message):
        client.stats()


def test_unreachable_api_names_the_url_and_the_fix():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiError, match=r"api\.test.*docker compose ps"):
        _client(handler).health()
