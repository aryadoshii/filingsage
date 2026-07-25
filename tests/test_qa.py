"""Cited Q&A tests — search() and both LLM provider calls are mocked; no
real network, no real Qdrant, no real Groq/Gemini calls anywhere in this
file. test_zero_results_never_calls_llm and
test_score_below_floor_never_calls_llm are the most important tests here:
they're the "never bluff" guarantee, and they must make ZERO calls to
either provider to prove it.
"""

from __future__ import annotations

import json
from datetime import date

import filingsage.gold.qa as qa
from filingsage.gold.retrieval import SearchResult


def _result(chunk_id: int, score: float, section: str = "risk_factors") -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        accession_number="acc-1",
        ticker="GOOGL",
        form_type="10-K",
        filed_at=date(2026, 1, 1),
        section=section,
        text="Some filing text discussing the matter at hand.",
        fusion_score=score,
    )


def test_zero_results_never_calls_llm(monkeypatch):
    monkeypatch.setattr(qa, "search", lambda *a, **k: [])
    calls: list[str] = []
    monkeypatch.setattr(qa, "_call_groq", lambda p: calls.append("groq") or "{}")
    monkeypatch.setattr(qa, "_call_gemini", lambda p: calls.append("gemini") or "{}")

    answer = qa.answer_question("What are the risks?")

    assert answer.insufficient_evidence is True
    assert answer.claims == []
    assert calls == []  # never touched an LLM


def test_score_below_floor_never_calls_llm(monkeypatch):
    monkeypatch.setattr(qa, "search", lambda *a, **k: [_result(1, qa.SCORE_FLOOR - 0.01)])
    calls: list[str] = []
    monkeypatch.setattr(qa, "_call_groq", lambda p: calls.append("groq") or "{}")
    monkeypatch.setattr(qa, "_call_gemini", lambda p: calls.append("gemini") or "{}")

    answer = qa.answer_question("What are the risks?")

    assert answer.insufficient_evidence is True
    assert calls == []


def test_normal_path_parses_answer_and_drops_hallucinated_citations(monkeypatch):
    results = [_result(1, 0.8), _result(2, 0.7)]
    monkeypatch.setattr(qa, "search", lambda *a, **k: results)

    raw = json.dumps({
        "answer": "The company faces competitive risk.",
        "claims": [
            {"text": "Competitive risk is disclosed.", "chunk_ids": [1]},
            {"text": "Hallucinated citation.", "chunk_ids": [999]},  # never retrieved
        ],
        "confidence": "high",
        "insufficient_evidence": False,
    })
    monkeypatch.setattr(qa, "_call_groq", lambda p: raw)

    def gemini_should_not_be_called(p):
        raise AssertionError("gemini should not be called when groq succeeds")

    monkeypatch.setattr(qa, "_call_gemini", gemini_should_not_be_called)

    answer = qa.answer_question("What are the risks?")

    assert answer.insufficient_evidence is False
    assert answer.confidence == "high"
    # the claim citing only a hallucinated chunk_id is dropped entirely
    assert len(answer.claims) == 1
    assert answer.claims[0].chunk_ids == [1]


def test_malformed_json_retries_then_falls_through_to_insufficient_evidence(monkeypatch):
    monkeypatch.setattr(qa, "search", lambda *a, **k: [_result(1, 0.8)])
    calls = {"groq": 0, "gemini": 0}

    def bad_groq(prompt):
        calls["groq"] += 1
        return "not json at all"

    def bad_gemini(prompt):
        calls["gemini"] += 1
        return "also not json"

    monkeypatch.setattr(qa, "_call_groq", bad_groq)
    monkeypatch.setattr(qa, "_call_gemini", bad_gemini)

    answer = qa.answer_question("What are the risks?")

    assert answer.insufficient_evidence is True
    assert calls["groq"] == 2  # one retry, same provider
    assert calls["gemini"] == 2  # falls through to gemini, retried there too


def test_provider_fallback_on_groq_failure(monkeypatch):
    monkeypatch.setattr(qa, "search", lambda *a, **k: [_result(1, 0.8)])

    def raising_groq(prompt):
        raise RuntimeError("simulated Groq outage")

    gemini_calls: list[str] = []

    def working_gemini(prompt):
        gemini_calls.append(prompt)
        return json.dumps({
            "answer": "Answered by Gemini.",
            "claims": [{"text": "A claim.", "chunk_ids": [1]}],
            "confidence": "medium",
            "insufficient_evidence": False,
        })

    monkeypatch.setattr(qa, "_call_groq", raising_groq)
    monkeypatch.setattr(qa, "_call_gemini", working_gemini)

    answer = qa.answer_question("What are the risks?")

    assert len(gemini_calls) == 1  # groq failure isn't retried — straight to gemini, once
    assert answer.answer == "Answered by Gemini."
    assert answer.confidence == "medium"
    assert answer.insufficient_evidence is False
