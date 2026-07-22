"""Qdrant Cloud client + hybrid (dense + sparse) collection management.

One collection, "filings", with two named vectors per point — a dense
BGE-small vector for semantic search and a sparse BM25 vector for lexical
search (spec §6: hybrid retrieval). Both live on the same point so a single
upsert and a single hybrid query cover both signals; there's no separate
BM25 service to keep in sync (README → Technical Decisions #3).
"""

from __future__ import annotations

import uuid
from datetime import date
from functools import lru_cache

from qdrant_client import QdrantClient, models

from filingsage.config import get_settings
from filingsage.db.models import Chunk as ChunkRow
from filingsage.gold.embedding import DENSE_VECTOR_SIZE, EMBED_BATCH_SIZE, embed_texts

COLLECTION_NAME = "filings"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Fixed namespace for deriving stable point ids from (accession_number,
# seq) — any constant UUID works, this just needs to be the same one every
# time so uuid5() is reproducible across processes/runs.
_POINT_ID_NAMESPACE = uuid.UUID("6f6a2b0e-6b8b-4f0a-8b0a-9e6b0e6b8b4f")


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    settings = get_settings()
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


def point_id_for(accession_number: str, seq: int) -> str:
    """Deterministic point id: re-embedding the same (accession, seq) always
    resolves to the same Qdrant point, so a re-run overwrites in place
    instead of creating a duplicate — the same idempotency the rest of the
    pipeline gets from ON CONFLICT DO NOTHING, applied to a store that has
    no such clause of its own.
    """
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, f"{accession_number}:{seq}"))


def ensure_collection(client: QdrantClient | None = None) -> None:
    """Create the `filings` collection if it doesn't exist. Idempotent —
    safe to call before every upsert (upsert_chunks does exactly that), not
    just once at startup, since the check is a single cheap API call.
    """
    client = client or get_client()
    if client.collection_exists(COLLECTION_NAME):
        return
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            DENSE_VECTOR_NAME: models.VectorParams(
                size=DENSE_VECTOR_SIZE, distance=models.Distance.COSINE
            ),
        },
        sparse_vectors_config={
            # Qdrant/bm25 emits raw term frequency, not IDF-weighted scores
            # (see gold/embedding.py) — Modifier.IDF tells Qdrant to apply
            # IDF weighting itself, computed from this collection's own
            # corpus statistics, at query time.
            SPARSE_VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
    )


def upsert_chunks(
    chunk_rows: list[ChunkRow],
    *,
    accession_number: str,
    cik: int,
    ticker: str,
    form_type: str,
    filed_at: date,
    client: QdrantClient | None = None,
) -> dict[int, str]:
    """Embed each chunk row's text and upsert to Qdrant, in batches.

    The payload is fully denormalized (cik/ticker/form_type/filed_at come
    from the filing, not the chunk row) so filtered retrieval later doesn't
    need a join back to Postgres — everything needed to filter and cite a
    hit lives on the point itself.

    Processed EMBED_BATCH_SIZE chunks at a time — embed batch, upsert batch,
    continue — so peak memory is one batch's worth of embeddings, not the
    whole filing's (a large 10-K's ~40 chunks embedded in one call OOM-killed
    the worker in production — see embedding.py's module docstring for the
    measured numbers behind EMBED_BATCH_SIZE's value). Qdrant has no
    transactions: if a later
    batch fails, earlier batches are already durably upserted (harmlessly —
    point ids are stable, so a retry of the whole task just re-upserts them
    identically) and this function raises, which the caller (chunk_and_embed)
    relies on to keep status from advancing to EMBEDDED on a partial run.

    Returns {chunk_row.id: qdrant_point_id} for every row that was
    successfully upserted, so the caller can write qdrant_point_id back onto
    each `chunks` table row. On a mid-batch failure this never returns —
    the exception propagates before the caller can use a partial result.
    """
    client = client or get_client()
    ensure_collection(client)

    if not chunk_rows:
        return {}

    point_ids: dict[int, str] = {}
    for batch_start in range(0, len(chunk_rows), EMBED_BATCH_SIZE):
        batch = chunk_rows[batch_start : batch_start + EMBED_BATCH_SIZE]
        dense_vectors, sparse_vectors = embed_texts([row.text for row in batch])

        points: list[models.PointStruct] = []
        for row, dense, sparse in zip(batch, dense_vectors, sparse_vectors, strict=True):
            point_id = point_id_for(accession_number, row.seq)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector={
                        DENSE_VECTOR_NAME: dense,
                        SPARSE_VECTOR_NAME: models.SparseVector(
                            indices=sparse.indices, values=sparse.values
                        ),
                    },
                    payload={
                        "cik": cik,
                        "ticker": ticker,
                        "form_type": form_type,
                        "filed_at": filed_at.isoformat(),
                        "section": row.section,
                        "seq": row.seq,
                        "accession_number": accession_number,
                        "chunk_id": row.id,
                        "text": row.text,
                    },
                )
            )
            point_ids[row.id] = point_id

        client.upsert(collection_name=COLLECTION_NAME, points=points)

    return point_ids
