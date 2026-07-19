"""Dense + sparse embeddings via FastEmbed (ONNX, no torch) — spec §5/§6.

Both models run in-worker (README → Technical Decisions #23): the spec's
24GB self-hosted VM never existed, and FastEmbed's ONNX runtime fits the
512MB Fly worker where a full PyTorch stack wouldn't.

Dense: BAAI/bge-small-en-v1.5 — the same model gold/chunking.py's tokenizer
matches, 384-dim vectors (the size vector_store.py's collection is
configured for).

Sparse: Qdrant/bm25 — the lightest BM25-family model FastEmbed offers
(~10MB vs. SPLADE++'s ~530MB): plain term-frequency sparse vectors, no
neural inference. It's marked `requires_idf=True` in fastembed's own source
(fastembed/sparse/bm25.py) — it emits term frequency only and expects the
IDF weighting to come from the vector store, so vector_store.py's
collection sets `Modifier.IDF` on the sparse vector config to have Qdrant
apply that server-side from corpus statistics.

Both models are baked into the Docker image at build time (Dockerfile) —
no runtime download.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

from fastembed import SparseTextEmbedding, TextEmbedding

from filingsage.gold.chunking import MAX_CHUNK_TOKENS, count_tokens

logger = logging.getLogger(__name__)

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DENSE_VECTOR_SIZE = 384

SPARSE_MODEL = "Qdrant/bm25"


@lru_cache(maxsize=1)
def _dense_model() -> TextEmbedding:
    return TextEmbedding(DENSE_MODEL)


@lru_cache(maxsize=1)
def _sparse_model() -> SparseTextEmbedding:
    return SparseTextEmbedding(SPARSE_MODEL)


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]


def embed_texts(texts: list[str]) -> tuple[list[list[float]], list[SparseVector]]:
    """Embed a batch of chunk texts. Returns (dense_vectors, sparse_vectors),
    same order and length as `texts`.

    gold/chunking.py already sizes chunks to MAX_CHUNK_TOKENS, so nothing
    passed through the normal pipeline should ever exceed the model's
    budget — but silently trusting that invariant here would let a future
    chunking regression (or a chunk built by hand, e.g. in a test) truncate
    quietly inside the ONNX runtime. We check first and log loudly instead.
    """
    for text in texts:
        n = count_tokens(text)
        if n > MAX_CHUNK_TOKENS:
            logger.error(
                "embed_texts: input has %d tokens, exceeds MAX_CHUNK_TOKENS "
                "(%d) — should have been caught at chunk time; the embedding "
                "model will truncate this input.",
                n, MAX_CHUNK_TOKENS,
            )

    dense_vectors = [vec.tolist() for vec in _dense_model().embed(texts)]
    sparse_vectors = [
        SparseVector(indices=vec.indices.tolist(), values=vec.values.tolist())
        for vec in _sparse_model().embed(texts)
    ]
    return dense_vectors, sparse_vectors
