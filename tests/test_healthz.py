"""API smoke tests — run in-process via TestClient, no containers needed."""

from fastapi.testclient import TestClient

from filingsage.api.main import app

client = TestClient(app)


def test_healthz_returns_ok() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "filingsage-api"
    assert "version" in body
