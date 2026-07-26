"""POST /qa tests — run in-process via TestClient. answer_question() and
check_rate_limit() are monkeypatched per test so these run with no real
LLM/Qdrant/Redis calls, except test_rate_limit_returns_429_after_max_requests
below, which uses a real Redis via testcontainers specifically to prove the
real rate limiter <-> endpoint integration, not just that check_rate_limit()
works standalone (already covered by manual verification of rate_limit.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from testcontainers.redis import RedisContainer

import filingsage.api.main as main
import filingsage.api.rate_limit as rate_limit
from filingsage.gold.qa import Answer, Claim

client = TestClient(main.app)

_ANSWER = Answer(
    answer="Apple faces competitive risk from price pressure.",
    claims=[Claim(text="Price competition pressures margins.", chunk_ids=[1, 2])],
    confidence="high",
    insufficient_evidence=False,
)


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch):
    """Tests other than the rate-limit one shouldn't need real Redis or
    care about it — always-allow by default; the rate-limit test overrides
    this itself with a real limiter.
    """
    monkeypatch.setattr(main, "check_rate_limit", lambda ip: True)


def test_valid_request_returns_200_with_answer_shape(monkeypatch):
    monkeypatch.setattr(main, "answer_question", lambda *a, **k: _ANSWER)

    resp = client.post("/qa", json={"question": "What are Apple's risks?", "ticker": "AAPL"})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "answer": _ANSWER.answer,
        "claims": [{"text": "Price competition pressures margins.", "chunk_ids": [1, 2]}],
        "confidence": "high",
        "insufficient_evidence": False,
    }


def test_empty_question_returns_422():
    resp = client.post("/qa", json={"question": ""})
    assert resp.status_code == 422


def test_too_long_question_returns_422():
    resp = client.post("/qa", json={"question": "x" * 501})
    assert resp.status_code == 422


def test_question_at_max_length_is_accepted(monkeypatch):
    monkeypatch.setattr(main, "answer_question", lambda *a, **k: _ANSWER)
    resp = client.post("/qa", json={"question": "x" * 500})
    assert resp.status_code == 200


def test_answer_question_raising_returns_clean_503(monkeypatch):
    def raising(*args, **kwargs):
        raise RuntimeError("simulated Qdrant outage with a sensitive internal detail")

    monkeypatch.setattr(main, "answer_question", raising)

    resp = client.post("/qa", json={"question": "What are the risks?"})

    assert resp.status_code == 503
    body_text = resp.text
    assert "simulated Qdrant outage" not in body_text
    assert "RuntimeError" not in body_text
    assert "Traceback" not in body_text
    assert resp.json() == {"detail": "Q&A is temporarily unavailable"}


def test_rate_limiter_raising_returns_clean_503(monkeypatch):
    """Regression test for a real production bug: check_rate_limit() used
    to be called OUTSIDE the route's try/except entirely, so its own
    failure (e.g. a Redis connection/config error) bypassed all error
    handling and surfaced as a raw, unhandled 500 instead of the same clean
    503 answer_question() failures get. Mirrors
    test_answer_question_raising_returns_clean_503 above, but for the rate
    limiter's own failure path.
    """
    def raising(ip):
        raise RuntimeError("simulated Redis TLS misconfiguration with a sensitive internal detail")

    monkeypatch.setattr(main, "check_rate_limit", raising)
    monkeypatch.setattr(main, "answer_question", lambda *a, **k: _ANSWER)

    resp = client.post("/qa", json={"question": "What are the risks?"})

    assert resp.status_code == 503
    body_text = resp.text
    assert "simulated Redis TLS" not in body_text
    assert "RuntimeError" not in body_text
    assert "Traceback" not in body_text
    assert resp.json() == {"detail": "Q&A is temporarily unavailable"}


@pytest.mark.integration
def test_rate_limit_returns_429_after_max_requests(monkeypatch):
    monkeypatch.setattr(main, "answer_question", lambda *a, **k: _ANSWER)

    with RedisContainer() as redis_container:
        real_redis = redis_container.get_client()
        # Bypass the module-level lru_cache singleton entirely — point the
        # route's check_rate_limit straight at this test's real client via
        # the function's own injection seam.
        monkeypatch.setattr(
            main, "check_rate_limit",
            lambda ip: rate_limit.check_rate_limit(ip, redis_client=real_redis),
        )

        responses = [
            client.post("/qa", json={"question": "What are the risks?"})
            for _ in range(rate_limit.RATE_LIMIT_MAX_REQUESTS + 1)
        ]

    statuses = [r.status_code for r in responses]
    assert statuses[:-1] == [200] * rate_limit.RATE_LIMIT_MAX_REQUESTS
    assert statuses[-1] == 429
