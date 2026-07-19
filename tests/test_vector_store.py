"""Vector store tests — qdrant-client's in-memory mode (QdrantClient(":memory:")),
never the real Qdrant Cloud cluster. Embedding is the real FastEmbed models
(same as test_embedding.py); only the vector store is faked.
"""

from __future__ import annotations

from datetime import date

from qdrant_client import QdrantClient

from filingsage.db.models import Chunk as ChunkRow
from filingsage.gold.vector_store import (
    COLLECTION_NAME,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    ensure_collection,
    point_id_for,
    upsert_chunks,
)


def _chunk_row(id_: int, seq: int, section: str = "risk_factors", text: str | None = None) -> ChunkRow:
    return ChunkRow(
        id=id_,
        filing_id=1,
        section=section,
        seq=seq,
        text=text or f"Chunk text number {seq}, unique content to embed.",
        text_hash=f"hash-{seq}",
        char_count=40,
        token_count=8,
    )


def test_ensure_collection_is_idempotent():
    client = QdrantClient(":memory:")

    ensure_collection(client)
    assert client.collection_exists(COLLECTION_NAME)

    ensure_collection(client)  # second call: must not raise or recreate
    assert client.collection_exists(COLLECTION_NAME)

    info = client.get_collection(COLLECTION_NAME)
    assert DENSE_VECTOR_NAME in info.config.params.vectors
    assert SPARSE_VECTOR_NAME in info.config.params.sparse_vectors


def test_upsert_chunks_writes_both_vectors_and_full_payload():
    client = QdrantClient(":memory:")
    rows = [_chunk_row(101, seq=0), _chunk_row(102, seq=1, section="mdna")]

    point_ids = upsert_chunks(
        rows,
        accession_number="0000320193-26-000013",
        cik=320193,
        ticker="AAPL",
        form_type="10-Q",
        filed_at=date(2026, 5, 1),
        client=client,
    )

    assert set(point_ids.keys()) == {101, 102}

    points = client.retrieve(
        COLLECTION_NAME, ids=list(point_ids.values()), with_vectors=True, with_payload=True
    )
    assert len(points) == 2

    by_id = {p.id: p for p in points}
    point = by_id[point_ids[101]]
    assert len(point.vector[DENSE_VECTOR_NAME]) == 384
    assert len(point.vector[SPARSE_VECTOR_NAME].indices) > 0
    assert point.payload == {
        "cik": 320193,
        "ticker": "AAPL",
        "form_type": "10-Q",
        "filed_at": "2026-05-01",
        "section": "risk_factors",
        "seq": 0,
        "accession_number": "0000320193-26-000013",
        "chunk_id": 101,
        "text": rows[0].text,
    }


def test_reupserting_same_accession_and_seq_does_not_duplicate():
    client = QdrantClient(":memory:")
    row = _chunk_row(201, seq=0)

    first = upsert_chunks(
        [row], accession_number="acc-dup-test", cik=1, ticker="X",
        form_type="8-K", filed_at=date(2026, 1, 1), client=client,
    )
    second = upsert_chunks(
        [row], accession_number="acc-dup-test", cik=1, ticker="X",
        form_type="8-K", filed_at=date(2026, 1, 1), client=client,
    )

    assert first == second  # same point id both times
    count = client.count(COLLECTION_NAME, exact=True).count
    assert count == 1  # overwritten in place, not duplicated


def test_point_id_is_stable_for_the_same_accession_and_seq():
    assert point_id_for("acc-1", 5) == point_id_for("acc-1", 5)
    assert point_id_for("acc-1", 5) != point_id_for("acc-1", 6)
    assert point_id_for("acc-1", 5) != point_id_for("acc-2", 5)


def test_upsert_chunks_with_empty_list_is_a_noop():
    client = QdrantClient(":memory:")
    assert upsert_chunks([], accession_number="acc", cik=1, ticker="X",
                          form_type="8-K", filed_at=date(2026, 1, 1), client=client) == {}
