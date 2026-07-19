"""Embedding tests — real FastEmbed models (BGE-small dense + BM25 sparse),
no mocking. Same philosophy as tests/test_chunking.py: the whole point is
that these are the actual models used at runtime, so a mock would test
nothing meaningful. First run in a fresh environment downloads both models
(~70MB total) and caches them; subsequent runs are offline.
"""

from __future__ import annotations

from filingsage.gold.embedding import DENSE_VECTOR_SIZE, embed_texts

TEXTS = [
    "Item 1A. Risk Factors. The Company faces macroeconomic headwinds including "
    "inflation, elevated interest rates, and heightened competition.",
    "Item 7. Management's Discussion and Analysis. Net sales increased 5% year "
    "over year, driven by strong demand in the services segment.",
]


def test_dense_vectors_have_expected_dimension():
    dense_vectors, _ = embed_texts(TEXTS)
    assert len(dense_vectors) == len(TEXTS)
    assert all(len(v) == DENSE_VECTOR_SIZE == 384 for v in dense_vectors)


def test_sparse_vectors_are_non_empty():
    _, sparse_vectors = embed_texts(TEXTS)
    assert len(sparse_vectors) == len(TEXTS)
    for sv in sparse_vectors:
        assert len(sv.indices) > 0
        assert len(sv.values) == len(sv.indices)


def test_embedding_is_deterministic_for_the_same_input():
    dense_1, sparse_1 = embed_texts([TEXTS[0]])
    dense_2, sparse_2 = embed_texts([TEXTS[0]])

    assert dense_1 == dense_2
    assert sparse_1[0].indices == sparse_2[0].indices
    assert sparse_1[0].values == sparse_2[0].values


def test_batch_returns_the_right_count():
    texts = TEXTS + ["Item 9.01. Financial Statements and Exhibits."]
    dense_vectors, sparse_vectors = embed_texts(texts)
    assert len(dense_vectors) == len(texts)
    assert len(sparse_vectors) == len(texts)


def test_oversized_input_logs_error_but_still_embeds(caplog):
    # "word " repeated well past MAX_CHUNK_TOKENS (384) — the chunker should
    # never actually produce this, but embed_texts must not fail silently
    # or fail loudly (crash) if it ever receives one; it logs and truncates
    # via the model's own tokenizer, same as any oversized input would.
    oversized = "word " * 1000
    with caplog.at_level("ERROR"):
        dense_vectors, sparse_vectors = embed_texts([oversized])

    assert len(dense_vectors) == 1
    assert len(dense_vectors[0]) == DENSE_VECTOR_SIZE
    assert any("exceeds MAX_CHUNK_TOKENS" in r.message for r in caplog.records)
