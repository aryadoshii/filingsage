"""POST /internal/ingest — auth and dispatch, run in-process via TestClient.

get_settings and ingest_watchlist.delay are monkeypatched per test so these
run with no real config, no broker, and no Redis.
"""

from types import SimpleNamespace

from fastapi.testclient import TestClient

import filingsage.api.main as main

client = TestClient(main.app)


class _Result:
    def __init__(self, task_id: str):
        self.id = task_id


def _settings(ingest_token: str = "", default_universe=None) -> SimpleNamespace:
    return SimpleNamespace(
        ingest_token=ingest_token,
        default_universe=default_universe or ["AAPL", "MSFT", "NVDA"],
    )


def _record_delay(monkeypatch, calls: list, task_id: str = "fake-task-id") -> None:
    monkeypatch.setattr(
        main.ingest_watchlist,
        "delay",
        lambda *args, **kwargs: calls.append((args, kwargs)) or _Result(task_id),
    )


def test_ingest_trigger_503_when_token_not_configured(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(ingest_token=""))
    calls: list = []
    _record_delay(monkeypatch, calls)

    resp = client.post("/internal/ingest", headers={"X-Ingest-Token": "anything"})

    assert resp.status_code == 503
    assert calls == []  # never dispatched — an unset secret must fail closed


def test_ingest_trigger_401_when_header_missing(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(ingest_token="secret123"))
    calls: list = []
    _record_delay(monkeypatch, calls)

    resp = client.post("/internal/ingest")

    assert resp.status_code == 401
    assert calls == []


def test_ingest_trigger_401_when_token_wrong(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(ingest_token="secret123"))
    calls: list = []
    _record_delay(monkeypatch, calls)

    resp = client.post("/internal/ingest", headers={"X-Ingest-Token": "wrong-token"})

    assert resp.status_code == 401
    assert calls == []


def test_ingest_trigger_202_defaults_to_universe(monkeypatch):
    monkeypatch.setattr(
        main, "get_settings",
        lambda: _settings(ingest_token="secret123", default_universe=["AAPL", "MSFT"]),
    )
    calls: list = []
    _record_delay(monkeypatch, calls, task_id="abc-123")

    resp = client.post("/internal/ingest", headers={"X-Ingest-Token": "secret123"})

    assert resp.status_code == 202
    assert resp.json() == {"task_id": "abc-123"}
    assert calls == [((["AAPL", "MSFT"], None), {})]


def test_ingest_trigger_202_with_explicit_tickers_and_limit(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(ingest_token="secret123"))
    calls: list = []
    _record_delay(monkeypatch, calls, task_id="xyz-789")

    resp = client.post(
        "/internal/ingest",
        headers={"X-Ingest-Token": "secret123"},
        json={"tickers": ["TSLA"], "limit": 3},
    )

    assert resp.status_code == 202
    assert resp.json() == {"task_id": "xyz-789"}
    assert calls == [((["TSLA"], 3), {})]
