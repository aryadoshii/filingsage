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

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

import filingsage.db.session as db_session
import filingsage.gold.qa as qa
from filingsage.db.models import Chunk as ChunkRow
from filingsage.db.models import Company, Filing
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


def test_rerank_is_called_and_determines_what_reaches_the_llm_prompt(monkeypatch):
    """Proves the wiring, not just that rerank() itself works (test_rerank.py
    covers that): answer_question() must call rerank() with the real
    search() results and LLM_CONTEXT_TOP_N, and the LLM prompt must reflect
    rerank's OUTPUT, not the raw search() order — a chunk search() returned
    but rerank() dropped must never reach the prompt.
    """
    excluded = _result(1, 0.9)  # ranks first by fusion score...
    kept = _result(2, 0.5)  # ...but rerank() below "promotes" this one instead
    monkeypatch.setattr(qa, "search", lambda *a, **k: [excluded, kept])

    rerank_calls = []

    def fake_rerank(query, chunks, *, top_k):
        rerank_calls.append((query, chunks, top_k))
        return [kept]  # only the "promoted" chunk survives reranking

    monkeypatch.setattr(qa, "rerank", fake_rerank)

    captured_prompts = []

    def capturing_groq(prompt):
        captured_prompts.append(prompt)
        return json.dumps({
            "answer": "Answer grounded in the reranked chunk.",
            "claims": [{"text": "A claim.", "chunk_ids": [2]}],
            "confidence": "high",
            "insufficient_evidence": False,
        })

    monkeypatch.setattr(qa, "_call_groq", capturing_groq)

    answer = qa.answer_question("What are the risks?")

    assert len(rerank_calls) == 1
    called_query, called_chunks, called_top_k = rerank_calls[0]
    assert called_query == "What are the risks?"
    assert called_chunks == [excluded, kept]
    assert called_top_k == qa.LLM_CONTEXT_TOP_N

    assert len(captured_prompts) == 1
    assert f"chunk_id={kept.chunk_id}" in captured_prompts[0]
    assert f"chunk_id={excluded.chunk_id}" not in captured_prompts[0]
    assert answer.claims[0].chunk_ids == [2]


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


@pytest.mark.integration
def test_resolve_citations_round_trips_a_real_chunk_id(monkeypatch):
    """The full round-trip a mocked test can't cover: a chunk_id that
    appears in a real Claim must actually resolve via resolve_citations()'s
    real query against a real chunks/filings/companies join — not just be
    among the ids that were retrieved (test_normal_path... above already
    covers that half). Real Postgres via testcontainers, real alembic
    migrations, same pattern as tests/test_pipeline.py.

    This is exactly the failure mode found running the CLI for real: a
    chunk_id can be completely genuine and still resolve to nothing if
    it's looked up against the wrong database. The second assertion below
    (a chunk_id with no matching row) encodes that as expected, documented
    behavior — resolve_citations() must never raise for it.
    """
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        engine = create_engine(url)

        monkeypatch.setattr(db_session, "_engine", engine)
        monkeypatch.setattr(
            db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
        )

        with db_session.session_scope() as session:
            session.add(Company(cik=999999, ticker="AAPL", name="Apple Inc."))
            filing = Filing(
                cik=999999, accession_no="acc-citation-test", form_type="10-K",
                filed_at=date(2026, 5, 1), primary_document="a.htm",
            )
            session.add(filing)
            session.flush()
            chunk = ChunkRow(
                filing_id=filing.id, section="risk_factors", seq=0,
                text="Competitive risk text.", text_hash="hash-1",
                char_count=22, token_count=5,
            )
            session.add(chunk)
            session.flush()
            real_chunk_id = chunk.id

        citations = qa.resolve_citations([real_chunk_id, 999_999_999])

        assert real_chunk_id in citations
        citation = citations[real_chunk_id]
        assert citation.ticker == "AAPL"
        assert citation.form_type == "10-K"
        assert citation.filed_at == date(2026, 5, 1)
        assert citation.section == "risk_factors"
        assert citation.accession_number == "acc-citation-test"

        # a genuine-looking id with no matching row: absent, not an error
        assert 999_999_999 not in citations
