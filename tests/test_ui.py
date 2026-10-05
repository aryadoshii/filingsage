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


# --- numbers and statements ------------------------------------------------------


def test_money_uses_finance_scales_and_a_real_minus_sign():
    assert fmt.money(391_035_000_000) == "$391.0B"
    assert fmt.money(1_234_000_000_000) == "$1.2T"
    assert fmt.money(512_300_000) == "$512.3M"
    assert fmt.money(12_345) == "$12,345"
    assert fmt.money(-2_100_000_000) == "−$2.1B"
    assert fmt.money(None) == "—"


def test_percentages_and_per_share_values():
    assert fmt.pct(0.2456) == "24.6%"
    assert fmt.pct(0.12, signed=True) == "+12.0%"
    assert fmt.pct(-0.041, signed=True) == "−4.1%"
    assert fmt.per_share(6.4) == "$6.40"
    assert fmt.per_share(None) == "—"


def test_period_labels_use_the_month_the_period_ended():
    assert fmt.period_label("2025-09-27", "quarter") == "Sep 2025"
    assert fmt.period_label("2025-09-27", "annual") == "FY Sep 2025"


def test_statement_csv_has_raw_numbers_and_skips_section_headings():
    columns = [
        {"end": "2025-06-28", "values": {"revenue": 120.0, "net_margin": None}, "derived": []},
        {"end": "2025-09-27", "values": {"revenue": 136.0, "net_margin": 0.2}, "derived": []},
    ]
    lines = fmt.statement_csv(columns, "quarter").splitlines()

    assert lines[0] == "metric,Jun 2025,Sep 2025"
    assert "Revenue,120.0,136.0" in lines
    assert "Net margin,,0.2" in lines  # missing value is blank, not zero
    assert not any(line.startswith("Income statement") for line in lines)


def test_recent_filings_are_flagged_for_a_week():
    from datetime import date
    assert fmt.is_recent("2026-10-01", today=date(2026, 10, 4))
    assert not fmt.is_recent("2026-09-20", today=date(2026, 10, 4))
    assert not fmt.is_recent(None)


# --- pipeline health: event labels, dependency status, new client calls ------

_SRC = __import__("pathlib").Path(__file__).resolve().parent.parent / "src" / "filingsage"


def _emitted_event_types() -> set[str]:
    """Every event type string the backend source mentions in quotes."""
    import re

    pattern = re.compile(r'"((?:filing|company|ingest|pipeline)\.[a-z_]+)"')
    return {m for path in _SRC.rglob("*.py") for m in pattern.findall(path.read_text())}


def test_every_event_the_backend_emits_has_a_plain_language_label():
    """'Nothing raw like "company.refreshed" may appear in the UI' — checked
    against the backend's own source, so a new event type can't slip in."""
    types = _emitted_event_types()
    assert {"company.refreshed", "filing.failed", "pipeline.reconciled"} <= types  # scan works
    for event_type in types:
        label = fmt.event_label(event_type, {"step": "fetch"})
        assert "." not in label and "_" not in label, f"{event_type} shows as {label!r}"


def test_new_event_labels():
    assert fmt.event_label("company.refreshed") == "Financials refreshed"
    assert fmt.event_label("company.refresh_failed") == "Financials refresh failed"
    assert fmt.event_label("ingest.completed") == "Checked EDGAR"
    assert fmt.event_label("filing.requeued") == "Retried"
    assert fmt.event_label("pipeline.reconciled") == "Checked for stuck filings"
    assert fmt.event_label("filing.failed", {"step": "fetch"}) == "Failed at download"
    assert fmt.event_label("filing.failed", {"step": "embed"}) == "Failed at indexing"
    # An event type the dashboard doesn't know yet still reads as words.
    assert fmt.event_label("analysis.completed") == "Analysis completed"


def test_new_event_details():
    def detail(kind: str, payload: dict) -> str:
        return fmt.event_detail({"type": kind, "payload": payload})

    assert detail("company.refreshed", {"facts": 412}) == "412 financial facts"
    assert detail("ingest.completed", {"inserted": 1}) == "1 new filing"
    assert detail("ingest.completed", {"inserted": 0}) == "0 new filings"
    assert detail("filing.failed", {"error": "ConnectError: down", "attempts": 5}) == (
        "ConnectError: down (after 5 attempts)"
    )
    assert detail("company.refresh_failed", {"error": "HTTPStatusError: 404", "attempts": 1}) == (
        "HTTPStatusError: 404"
    )
    assert detail("filing.requeued", {"from_status": "parsed"}) == "Was stuck at: Parsed"
    assert detail("filing.recovery_reset", {"reason": "bronze missing on disk"}).startswith(
        "Downloaded file was missing"
    )
    assert detail("pipeline.reconciled", {"requeued": 0}) == "Nothing stuck"
    assert detail("pipeline.reconciled", {"requeued": 3, "needs_attention": 1}) == (
        "3 stuck filings retried; 1 need attention"
    )


def test_event_subjects_name_groups_in_words():
    assert fmt.event_subject({"entity_id": "watchlist"}) == "All tracked companies"
    assert fmt.event_subject({"entity_id": "pipeline"}) == "All filings"
    assert fmt.event_subject({"entity_id": "AAPL"}) == "AAPL"


def test_dependency_status_treats_anything_but_ok_as_down():
    assert fmt.dependency_status({"postgres": "ok", "redis": "down"}) == [
        ("Postgres", True), ("Redis", False), ("Qdrant", False),  # unreported = down
    ]


def test_readiness_returns_the_body_even_when_the_api_says_not_ready():
    body = {"postgres": "ok", "redis": "down", "qdrant": "ok"}
    api = _client(lambda request: httpx.Response(503, json=body))
    assert api.readiness() == body


def test_a_503_from_other_endpoints_is_still_an_error():
    api = _client(lambda request: httpx.Response(503, json={"detail": "Q&A is unavailable."}))
    with pytest.raises(ApiError):
        api.stats()


def test_needs_attention_calls_its_endpoint():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=[])

    assert _client(handler).needs_attention(limit=10) == []
    assert seen == [("/filings/needs-attention", {"limit": "10"})]
