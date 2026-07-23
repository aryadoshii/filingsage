"""Hybrid retrieval tests — qdrant-client's in-memory mode, real FastEmbed
models (same philosophy as test_embedding.py/test_vector_store.py: a mocked
embedder would prove nothing about whether dense/sparse/fusion actually
rank the way the whole design depends on them ranking). Never the real
Qdrant Cloud cluster.
"""

from __future__ import annotations

from datetime import date

from qdrant_client import QdrantClient, models

from filingsage.gold.embedding import embed_texts
from filingsage.gold.retrieval import search
from filingsage.gold.vector_store import (
    COLLECTION_NAME,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    ensure_collection,
)


def _seed(client: QdrantClient, chunks: list[dict]) -> None:
    ensure_collection(client)
    texts = [c["text"] for c in chunks]
    dense_vectors, sparse_vectors = embed_texts(texts)
    points = []
    for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors, strict=True):
        points.append(
            models.PointStruct(
                id=chunk["chunk_id"],
                vector={
                    DENSE_VECTOR_NAME: dense,
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=sparse.indices, values=sparse.values
                    ),
                },
                payload={
                    "cik": chunk.get("cik", 1),
                    "ticker": chunk["ticker"],
                    "form_type": chunk["form_type"],
                    "filed_at": chunk["filed_at"].isoformat(),
                    "section": chunk.get("section", "other_events"),
                    "seq": chunk.get("seq", 0),
                    "accession_number": chunk["accession_number"],
                    "chunk_id": chunk["chunk_id"],
                    "text": chunk["text"],
                },
            )
        )
    client.upsert(collection_name=COLLECTION_NAME, points=points)


def test_dense_ranks_semantically_close_chunk_with_zero_shared_keywords():
    client = QdrantClient(":memory:")
    _seed(
        client,
        [
            {
                "chunk_id": 1,
                "ticker": "AAPL",
                "form_type": "10-K",
                "filed_at": date(2026, 1, 1),
                "accession_number": "acc-1",
                # Paraphrase of the query below with essentially no
                # overlapping vocabulary.
                "text": "Automobile manufacturing output declined due to a component shortage.",
            },
            {
                "chunk_id": 2,
                "ticker": "AAPL",
                "form_type": "10-K",
                "filed_at": date(2026, 1, 1),
                "accession_number": "acc-2",
                "text": "The board declared a quarterly cash dividend payable to shareholders of record.",
            },
        ],
    )

    query = "vehicle production fell because chips remain scarce"
    assert not (set(query.lower().split()) & set(
        "Automobile manufacturing output declined due to a component shortage.".lower().split()
    ))

    results = search(query, limit=10, client=client)

    assert results
    assert results[0].chunk_id == 1


def test_sparse_ranks_exact_rare_term_match_above_semantic_distractor():
    client = QdrantClient(":memory:")
    _seed(
        client,
        [
            {
                "chunk_id": 10,
                "ticker": "TSLA",
                "form_type": "10-Q",
                "filed_at": date(2026, 1, 1),
                "accession_number": "acc-10",
                # Contains the exact rare figure, but is NOT conceptually
                # about impairment — a dense-only search would have no
                # semantic reason to favor this over chunk 11.
                "text": (
                    "Miscellaneous administrative adjustments were recorded, "
                    "including a $47.3 million reclassification between "
                    "accounts, as part of routine year-end procedures."
                ),
            },
            {
                "chunk_id": 11,
                "ticker": "TSLA",
                "form_type": "10-Q",
                "filed_at": date(2026, 1, 1),
                "accession_number": "acc-11",
                # Topically about impairment charges (semantically closer
                # to the query's subject) but never states the figure.
                "text": (
                    "The Company recorded a significant impairment charge "
                    "during the fourth quarter related to a decline in the "
                    "fair value of long-lived assets, reflecting adverse "
                    "market conditions in the segment."
                ),
            },
        ],
    )

    results = search("$47.3 million impairment charge", limit=10, client=client)

    assert results
    assert results[0].chunk_id == 10


def test_ticker_filter_excludes_other_tickers():
    client = QdrantClient(":memory:")
    _seed(
        client,
        [
            {
                "chunk_id": 20,
                "ticker": "GOOGL",
                "form_type": "10-K",
                "filed_at": date(2026, 1, 1),
                "accession_number": "acc-20",
                "text": "Advertising revenue increased due to growth in search and cloud.",
            },
            {
                "chunk_id": 21,
                "ticker": "TSLA",
                "form_type": "10-K",
                "filed_at": date(2026, 1, 1),
                "accession_number": "acc-21",
                "text": "Advertising revenue increased due to growth in search and cloud.",
            },
        ],
    )

    results = search("advertising revenue growth", ticker="GOOGL", limit=10, client=client)

    assert results
    assert all(r.ticker == "GOOGL" for r in results)
    assert 21 not in {r.chunk_id for r in results}


def test_since_filter_excludes_earlier_filings():
    client = QdrantClient(":memory:")
    _seed(
        client,
        [
            {
                "chunk_id": 30,
                "ticker": "MSFT",
                "form_type": "10-K",
                "filed_at": date(2025, 1, 1),
                "accession_number": "acc-30",
                "text": "Operating margin expanded due to disciplined cost management.",
            },
            {
                "chunk_id": 31,
                "ticker": "MSFT",
                "form_type": "10-K",
                "filed_at": date(2026, 6, 1),
                "accession_number": "acc-31",
                "text": "Operating margin expanded due to disciplined cost management.",
            },
        ],
    )

    results = search(
        "operating margin expansion", since=date(2026, 1, 1), limit=10, client=client
    )

    assert results
    assert all(r.filed_at >= date(2026, 1, 1) for r in results)
    assert 30 not in {r.chunk_id for r in results}


def test_limit_is_respected():
    client = QdrantClient(":memory:")
    _seed(
        client,
        [
            {
                "chunk_id": 100 + i,
                "ticker": "NVDA",
                "form_type": "8-K",
                "filed_at": date(2026, 1, 1),
                "accession_number": f"acc-{100 + i}",
                "text": f"Data center revenue update number {i} for the reporting period.",
            }
            for i in range(8)
        ],
    )

    results = search("data center revenue update", limit=5, client=client)

    assert len(results) <= 5
