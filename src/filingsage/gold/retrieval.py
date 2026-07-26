"""Hybrid (dense + sparse) retrieval over the `filings` Qdrant collection — spec §6 step 2.

No LLM, no answer generation here — this proves retrieval quality standalone
before anything is built on top of it. Cited Q&A (later increment) is a
thin layer over `search()`, not a rewrite of it.

Fusion is Qdrant's native RRF (Reciprocal Rank Fusion), computed server-side
across a dense prefetch and a sparse prefetch — not hand-rolled, so the
fusion math is exactly what Qdrant's own hybrid-search docs describe and
gets any future improvements to that implementation for free.

The query text is embedded with the SAME `embed_texts()` used for chunks
(gold/embedding.py) — a query vector and a chunk vector are only comparable
if they came from the same model call path; a different tokenization or
preprocessing step here would silently degrade retrieval quality without
raising any error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from qdrant_client import QdrantClient, models

from filingsage.gold.embedding import embed_texts
from filingsage.gold.vector_store import (
    COLLECTION_NAME,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    get_client,
)

# Each prefetch (dense, sparse) pulls this many times the final `limit`
# candidates before RRF fuses them — RRF needs each ranking to actually
# have depth to draw from, or a modality that ranks its best hit just
# outside a too-tight prefetch window can't contribute it to the fusion at
# all. 4x is comfortably more than the final limit without asking Qdrant to
# rank the whole collection for every query.
PREFETCH_MULTIPLIER = 4


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk_id: int
    accession_number: str
    ticker: str
    form_type: str
    filed_at: date
    section: str
    text: str
    fusion_score: float
    # No per-modality (dense/sparse) score: Qdrant's fused ScoredPoint only
    # carries the single RRF score, not each prefetch's contribution —
    # querying each prefetch again separately just for debug numbers would
    # double the Qdrant calls per search, so this is the one score we have.
    rerank_score: float | None = None
    # None until gold/rerank.py's rerank() rescores this result — search()
    # itself never sets this. A frozen dataclass field, not a second type,
    # so the retrieve -> rerank pipeline is a list[SearchResult] ->
    # list[SearchResult] transform (dataclasses.replace()) rather than
    # converting between two near-identical shapes at the rerank boundary.


def _build_filter(
    ticker: str | None, form_type: str | None, since: date | None
) -> models.Filter | None:
    conditions: list[models.FieldCondition] = []
    if ticker is not None:
        conditions.append(
            models.FieldCondition(key="ticker", match=models.MatchValue(value=ticker))
        )
    if form_type is not None:
        conditions.append(
            models.FieldCondition(key="form_type", match=models.MatchValue(value=form_type))
        )
    if since is not None:
        conditions.append(
            models.FieldCondition(
                key="filed_at", range=models.DatetimeRange(gte=since.isoformat())
            )
        )
    return models.Filter(must=conditions) if conditions else None


def search(
    query: str,
    *,
    ticker: str | None = None,
    form_type: str | None = None,
    since: date | None = None,
    limit: int = 40,
    client: QdrantClient | None = None,
) -> list[SearchResult]:
    """Hybrid dense+sparse search over embedded chunks, RRF-fused, top `limit`.

    Filters apply to EACH prefetch individually (not as a top-level filter
    on the fused query) — tested directly against qdrant-client's
    in-memory mode: a filter passed only at the outer query_points() level
    is NOT applied when the query is a FusionQuery, so per-prefetch is the
    only construct confirmed to actually restrict results by
    ticker/form_type/since.
    """
    client = client or get_client()
    query_filter = _build_filter(ticker, form_type, since)

    dense_vectors, sparse_vectors = embed_texts([query])
    dense_vector = dense_vectors[0]
    sparse_vector = sparse_vectors[0]

    prefetch_limit = limit * PREFETCH_MULTIPLIER
    response = client.query_points(
        collection_name=COLLECTION_NAME,
        prefetch=[
            models.Prefetch(
                query=dense_vector,
                using=DENSE_VECTOR_NAME,
                limit=prefetch_limit,
                filter=query_filter,
            ),
            models.Prefetch(
                query=models.SparseVector(
                    indices=sparse_vector.indices, values=sparse_vector.values
                ),
                using=SPARSE_VECTOR_NAME,
                limit=prefetch_limit,
                filter=query_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )

    return [
        SearchResult(
            chunk_id=point.payload["chunk_id"],
            accession_number=point.payload["accession_number"],
            ticker=point.payload["ticker"],
            form_type=point.payload["form_type"],
            filed_at=date.fromisoformat(point.payload["filed_at"]),
            section=point.payload["section"],
            text=point.payload["text"],
            fusion_score=point.score,
        )
        for point in response.points
    ]
