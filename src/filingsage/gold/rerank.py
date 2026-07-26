"""Cross-encoder reranking over hybrid retrieval results — spec §6 step 3.

Replaces the crude "just take the top N of the RRF-fused order" cut
qa.py used before this existed: a cross-encoder scores the (query, chunk)
pair directly, which is a real relevance judgment, not a byproduct of two
independent retrieval signals getting fused by rank position.

Spec concern, resolved with a real measurement, not a guess: spec line 112
names BAAI/bge-reranker-base. Measured via resource.getrusage().ru_maxrss
(same method as decisions #23/#26) standalone, before wiring anything in:
loading it costs ~2041MB resident — more than double the worker's entire
1GB budget, and about 4x the API's. Not viable here. Measured two lighter
FastEmbed cross-encoders as alternatives (spec's own "or similar" wording
covers this):

  model                              on-disk   load delta   +40-doc rerank
  Xenova/ms-marco-MiniLM-L-6-v2       0.08GB    ~219MB       +61MB  (~280MB total)
  Xenova/ms-marco-MiniLM-L-12-v2      0.12GB    ~320MB       +70MB  (~390MB total)

Went with the L-6 variant: smallest measured footprint that's still a
standard, widely-used MS MARCO-trained cross-encoder, not a toy model.
Stays in FastEmbed/ONNX — no new ML framework, consistent with decision
#23's reasoning (that's the whole reason self-hosted embedding/rerank was
viable on small memory budgets in the first place). See README → Technical
Decisions #29 for the full writeup.

Runs in the API process (filingsage-api), not the worker: reranking
happens at request time, inside answer_question()'s synchronous path,
alongside retrieval and generation — a completely different code path from
the worker's async, ingestion-time chunk_and_embed. Moving it to the
worker would need a new synchronous cross-process call that doesn't exist
today; that's a bigger architectural change than "load a model somewhere
with more free RAM," and wasn't asked for.
"""

from __future__ import annotations

import dataclasses
from functools import lru_cache

from fastembed.rerank.cross_encoder import TextCrossEncoder

from filingsage.gold.retrieval import SearchResult

RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

# Placeholder, deliberately NOT wired into an active gate anywhere yet —
# unlike qa.py's SCORE_FLOOR (calibrated from real observed RRF fusion
# scores, README → Technical Decisions #28), there is no production rerank
# score distribution to calibrate against yet. This is also less
# straightforward than SCORE_FLOOR to calibrate blindly: cross-encoder
# output here is a raw, unbounded logit (observed range in manual testing:
# roughly -6 to 0 for a small synthetic batch), not a normalized [0, ~1]-ish
# fused score — a naive threshold picked without real data risks rejecting
# everything (if set near/above 0) or nothing (if set too low). Revisit
# once /qa has run rerank in production and there's a real distribution to
# look at, same as SCORE_FLOOR was.
RERANK_SCORE_FLOOR = 0.0


@lru_cache(maxsize=1)
def _reranker() -> TextCrossEncoder:
    return TextCrossEncoder(model_name=RERANK_MODEL)


def rerank(query: str, chunks: list[SearchResult], *, top_k: int = 8) -> list[SearchResult]:
    """Cross-encoder rerank of `chunks` against `query`; returns the top_k
    by rerank score, descending.

    Each returned SearchResult is the input one with rerank_score filled in
    (dataclasses.replace — SearchResult is frozen) — chunk_id, text, and
    every other field are unchanged, only the ordering and the new score
    are new information.
    """
    if not chunks:
        return []

    scores = list(_reranker().rerank(query, [chunk.text for chunk in chunks]))
    rescored = [
        dataclasses.replace(chunk, rerank_score=score)
        for chunk, score in zip(chunks, scores, strict=True)
    ]
    rescored.sort(key=lambda r: r.rerank_score, reverse=True)
    return rescored[:top_k]
