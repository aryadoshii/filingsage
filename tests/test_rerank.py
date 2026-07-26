"""Reranker tests — the real Xenova/ms-marco-MiniLM-L-6-v2 cross-encoder,
no mocking (same philosophy as test_embedding.py/test_retrieval.py: a
mocked scorer would prove nothing about whether reranking actually changes
result order the way the whole point of adding it depends on). First run
in a fresh environment downloads the model (~90MB) and caches it;
subsequent runs are offline.
"""

from __future__ import annotations

from datetime import date

from filingsage.gold.rerank import rerank
from filingsage.gold.retrieval import SearchResult


def _result(chunk_id: int, fusion_score: float, text: str) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        accession_number="acc-1",
        ticker="AAPL",
        form_type="10-K",
        filed_at=date(2026, 1, 1),
        section="risk_factors",
        text=text,
        fusion_score=fusion_score,
    )


def test_rerank_promotes_the_semantically_relevant_chunk_over_a_higher_fused_one():
    query = "What risks does the Company face from supply chain disruptions?"

    # Ranks FIRST by fusion score (i.e. the raw RRF order search() would
    # hand in) but is topically unrelated to the query.
    unrelated_but_fused_higher = _result(
        chunk_id=1,
        fusion_score=0.9,
        text=(
            "The Board of Directors declared a quarterly cash dividend of "
            "$0.24 per share, payable to shareholders of record as of the "
            "close of business on a date to be determined."
        ),
    )
    # Ranks LAST by fusion score but is exactly what the query is asking about.
    relevant_but_fused_lower = _result(
        chunk_id=2,
        fusion_score=0.1,
        text=(
            "The Company's manufacturing operations depend on a limited "
            "number of suppliers for critical components. Disruptions in "
            "the supply chain, including shortages of semiconductors or "
            "other key materials, could materially delay production, "
            "increase costs, and adversely affect results of operations."
        ),
    )

    # Fed in fusion-score order, as search() would return it.
    fused_order = [unrelated_but_fused_higher, relevant_but_fused_lower]

    results = rerank(query, fused_order, top_k=8)

    assert [r.chunk_id for r in results] == [2, 1]
    assert results[0].rerank_score > results[1].rerank_score


def test_top_k_is_respected():
    query = "supply chain risk"
    chunks = [
        _result(i, fusion_score=0.5, text=f"Generic filler disclosure text number {i}.")
        for i in range(20)
    ]

    results = rerank(query, chunks, top_k=8)

    assert len(results) == 8
    assert all(r.rerank_score is not None for r in results)
    # sorted descending by rerank score
    scores = [r.rerank_score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_empty_input_returns_empty_list():
    assert rerank("any query", [], top_k=8) == []
