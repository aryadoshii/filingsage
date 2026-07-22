"""Dense + sparse embeddings via FastEmbed (ONNX, no torch) — spec §5/§6.

Both models run in-worker (README → Technical Decisions #23): the spec's
24GB self-hosted VM never existed, and FastEmbed's ONNX runtime fits the
Fly worker where a full PyTorch stack wouldn't.

Dense: BAAI/bge-small-en-v1.5 — the same model gold/chunking.py's tokenizer
matches, 384-dim vectors (the size vector_store.py's collection is
configured for).

Sparse: Qdrant/bm25 — the lightest BM25-family model FastEmbed offers
(~10MB vs. SPLADE++'s ~530MB): plain term-frequency sparse vectors, no
neural inference — fastembed's own bm25.py gives it `model_file="mock.file"`,
i.e. there's no ONNX session at all, just tokenize+stem+hash+scale. It's
marked `requires_idf=True` in that same source — it emits term frequency
only and expects the IDF weighting to come from the vector store, so
vector_store.py's collection sets `Modifier.IDF` on the sparse vector
config to have Qdrant apply that server-side from corpus statistics.

Measured evidence (README → Technical Decisions #26) — resident memory,
cumulative, one process: bare interpreter 13MB -> +full worker import
(everything filingsage.worker.tasks pulls in: Celery, SQLAlchemy, pyarrow,
duckdb, fastembed's own import) 158MB -> +dense model loaded 392MB ->
+sparse model ALSO loaded 392MB (+0.2MB — sparse is effectively free, per
the no-ONNX-session note above) -> +one embed call, batch of 8, 495MB.
Loading dense and sparse "one at a time" instead of both resident would
save that ~0.2MB — not a real lever, because the dense model was never
sharing the memory bill with a comparably expensive peer. If this budget
ever needs to shrink, dropping DENSE (keep BM25-only lexical search) frees
real memory; dropping sparse frees almost none. EMBED_BATCH_SIZE below is
the lever that actually moves the number, because it scales with batch
size where the model-residency cost doesn't.

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

# Caps peak memory: embedding a batch of texts allocates proportionally to
# batch size (the dense ONNX runtime holds every text's intermediate
# tensors in memory at once within a call — sparse's cost doesn't scale
# meaningfully either way, see module docstring). A large 10-K can produce
# ~40 chunks — embedding all of them in one embed_texts() call, on top of
# the loaded dense model plus the pyarrow/duckdb/Celery baseline, OOM-killed
# the worker in production (measured: batch-of-40 added ~480MB on top of an
# already ~390MB baseline, ~875MB total — see module docstring's numbers).
# 8 was the first fix; measured peak at 8 (~495MB total) leaves real but
# not huge headroom under the 1GB ceiling (README → Technical Decisions
# #26), so this is 4 for extra margin against whatever the gap is between
# these measurements (taken on a Mac) and the actual Linux container.
EMBED_BATCH_SIZE = 4


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
