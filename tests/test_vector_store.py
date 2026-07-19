"""Vector store tests — qdrant-client's in-memory mode (QdrantClient(":memory:")),
never the real Qdrant Cloud cluster. Embedding is the real FastEmbed models
(same as test_embedding.py); only the vector store is faked.
"""

from __future__ import annotations

from datetime import date

import pytest
from qdrant_client import QdrantClient

from filingsage.db.models import Chunk as ChunkRow
from filingsage.gold.embedding import EMBED_BATCH_SIZE
from filingsage.gold.vector_store import (
    COLLECTION_NAME,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    ensure_collection,
    point_id_for,
    upsert_chunks,
)


class _FlakyClient:
    """Proxies a real QdrantClient; raises on the Nth call to .upsert(),
    succeeds on every other call. Everything else (collection_exists,
    create_collection, count, retrieve, ...) passes straight through, so
    the caller sees a real in-memory Qdrant except for the one failure.
    """

    def __init__(self, inner: QdrantClient, fail_at_call: int):
        self._inner = inner
        self._fail_at_call = fail_at_call
        self.upsert_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def upsert(self, *args, **kwargs):
        self.upsert_calls += 1
        if self.upsert_calls == self._fail_at_call:
            raise RuntimeError("simulated Qdrant upsert failure")
        return self._inner.upsert(*args, **kwargs)


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


def test_upsert_chunks_processes_more_than_one_batch():
    client = QdrantClient(":memory:")
    n = EMBED_BATCH_SIZE + 4  # forces >=2 batches at the default batch size
    rows = [_chunk_row(1000 + i, seq=i) for i in range(n)]

    point_ids = upsert_chunks(
        rows, accession_number="acc-multi-batch", cik=1, ticker="X",
        form_type="10-K", filed_at=date(2026, 1, 1), client=client,
    )

    assert len(point_ids) == n
    assert set(point_ids.keys()) == {row.id for row in rows}
    count = client.count(COLLECTION_NAME, exact=True).count
    assert count == n  # every chunk landed, across every batch


def test_failure_on_a_later_batch_leaves_earlier_batches_upserted_but_raises():
    real_client = QdrantClient(":memory:")
    n = EMBED_BATCH_SIZE + 4  # 2 batches at the default batch size
    rows = [_chunk_row(2000 + i, seq=i) for i in range(n)]

    # Fail on the 2nd .upsert() call — the 1st batch (EMBED_BATCH_SIZE
    # chunks) succeeds before the 2nd batch fails.
    flaky = _FlakyClient(real_client, fail_at_call=2)

    with pytest.raises(RuntimeError, match="simulated Qdrant upsert failure"):
        upsert_chunks(
            rows, accession_number="acc-partial-failure", cik=1, ticker="X",
            form_type="10-K", filed_at=date(2026, 1, 1), client=flaky,
        )

    assert flaky.upsert_calls == 2
    # Qdrant isn't transactional: the first batch's points are durably
    # there despite the overall call raising. Harmless — point ids are
    # stable, so a retry re-upserts them identically rather than duplicating.
    count = real_client.count(COLLECTION_NAME, exact=True).count
    assert count == EMBED_BATCH_SIZE
