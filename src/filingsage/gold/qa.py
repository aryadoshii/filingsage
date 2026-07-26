"""Cited Q&A over embedded filing chunks — spec §6 step 4, straight RAG.

Spec line 217: "cited Q&A endpoint v0 (no agents yet — straight RAG)". This
builds directly on gold/retrieval.py's search() and gold/rerank.py's
rerank() — retrieval proved itself standalone in increment 3, so this is
the thin generation layer on top of it, not a rewrite. The full NLI
verification layer (spec step 5) is still deferred (README → Technical
Decisions #24).

"Never bluff" is the core guarantee: if retrieval didn't find anything
worth answering from, we say so WITHOUT calling an LLM at all (cheap, fast,
and can't hallucinate what it's never asked to generate). If it did, every
claim in the answer must cite the chunk_id(s) it came from, and citations
to chunk_ids the model was never shown are dropped, not trusted.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from filingsage.config import get_settings
from filingsage.db.models import Chunk as ChunkRow
from filingsage.db.models import Company, Filing
from filingsage.db.session import session_scope
from filingsage.gold.rerank import rerank
from filingsage.gold.retrieval import SearchResult, search

logger = logging.getLogger(__name__)

# Groq's own recommended replacement for the spec's named model
# (llama-3.3-70b-versatile) — Groq deprecated that model for free/
# developer-tier usage on 2026-06-17 (confirmed via Groq's docs/changelog
# before wiring this up, not assumed: CLAUDE.md's "free tiers drift" rule).
# Spec concern raised to Arya alongside this change. This is the closest
# replacement Groq itself recommends; swap freely if their lineup moves
# again — it's the one line to change.
GROQ_MODEL = "openai/gpt-oss-120b"

# Current (July 2026) supported Gemini Flash generation — gemini-2.0-flash
# has been shut down. Deliberately not the bleeding-edge 3.6 Flash that
# shipped days before this was written: not enough runway yet to be
# confident in its rollout stability for a fallback path. Swap freely.
GEMINI_MODEL = "gemini-3.5-flash"

# Retrieval still pulls the spec's full top-40 (search()'s own default), so
# filter/scope behavior stays exactly as specced — but only this many
# chunks, AFTER reranking, actually go into the LLM prompt. Dumping 40
# chunks into one prompt would blow past useful relevance long before it
# blows past token limits.
LLM_CONTEXT_TOP_N = 8

# "Never bluff" gate: if the top hybrid-fused result scores below this,
# treat it as "nothing relevant indexed" and skip the LLM call (and the
# rerank call) entirely. Runs on the RAW fusion score, BEFORE reranking —
# deliberately, on two grounds: (1) it's already calibrated from real
# production RRF scores (increment 3's manual eyeballing, roughly 0.15-0.83
# for genuinely relevant top hits), while gold/rerank.py's own
# RERANK_SCORE_FLOOR is an uncalibrated placeholder with no production
# distribution behind it yet — gating on the trusted signal first is more
# honest than gating on the untrusted one. (2) It's cheap; reranking loads
# and runs a ~280MB cross-encoder, so filtering obviously-irrelevant
# queries out before paying that cost (rather than after) is the right
# order regardless of calibration. Trivially tunable.
SCORE_FLOOR = 0.1

_INSUFFICIENT_EVIDENCE_MESSAGE = (
    "I don't have enough information in the filings I've indexed to answer this."
)


class Claim(BaseModel):
    text: str
    chunk_ids: list[int]


class Answer(BaseModel):
    answer: str
    claims: list[Claim] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    insufficient_evidence: bool = False


@dataclass(frozen=True, slots=True)
class ChunkCitation:
    """A claim's chunk_id, joined back to the filing/company metadata
    needed to actually display a citation (ticker, form, date, section) —
    the `chunks` table itself only stores section/seq/text/hashes, not
    ticker/form_type/filed_at, so displaying a citation always means
    joining back to `filings`/`companies`.
    """

    chunk_id: int
    ticker: str
    form_type: str
    filed_at: date
    section: str
    accession_number: str


def resolve_citations(chunk_ids: list[int]) -> dict[int, ChunkCitation]:
    """Join chunk_ids back to filing/company metadata for display.

    Library-level, not CLI-level, on purpose: the CLI's claim printout and
    the eventual API response both need this exact join, and this project's
    own convention is that logic like this lives where tests can reach it,
    not inline in a command handler.

    Returns only the ids that actually resolved — a chunk_id with no
    matching row is simply absent from the result, never an error. That's
    not a hypothetical: it's exactly what happens if this is queried
    against a different Postgres than the one the chunk_ids' embeddings
    were actually upserted from (e.g. a local DATABASE_URL while search()
    is pointed at a production Qdrant cluster) — the ids are real, just not
    in the database being asked.
    """
    if not chunk_ids:
        return {}
    with session_scope() as session:
        rows = session.execute(
            select(
                ChunkRow.id, ChunkRow.section, Filing.accession_no,
                Filing.form_type, Filing.filed_at, Company.ticker,
            )
            .join(Filing, Filing.id == ChunkRow.filing_id)
            .join(Company, Company.cik == Filing.cik)
            .where(ChunkRow.id.in_(chunk_ids))
        ).all()
    return {
        row.id: ChunkCitation(
            chunk_id=row.id,
            ticker=row.ticker,
            form_type=row.form_type,
            filed_at=row.filed_at,
            section=row.section,
            accession_number=row.accession_no,
        )
        for row in rows
    }


def _insufficient_evidence() -> Answer:
    return Answer(
        answer=_INSUFFICIENT_EVIDENCE_MESSAGE,
        claims=[],
        confidence="low",
        insufficient_evidence=True,
    )


def _build_prompt(query: str, results: list[SearchResult]) -> str:
    context = "\n\n".join(
        f"[chunk_id={r.chunk_id} ticker={r.ticker} form={r.form_type} "
        f"filed_at={r.filed_at.isoformat()} section={r.section}]\n{r.text}"
        for r in results
    )
    return (
        "You are a research analyst answering a question using ONLY the SEC "
        "filing excerpts below. Every claim in your answer must cite the "
        "chunk_id(s) of the excerpt(s) it is based on. If the excerpts don't "
        "contain enough information to answer, say so honestly rather than "
        "guessing.\n\n"
        f"Question: {query}\n\n"
        f"Filing excerpts:\n{context}\n\n"
        "Respond with ONLY a JSON object matching this shape, no other text:\n"
        '{"answer": "<answer text>", '
        '"claims": [{"text": "<specific claim>", "chunk_ids": [<int>, ...]}], '
        '"confidence": "high" | "medium" | "low", '
        '"insufficient_evidence": <true if the excerpts do not answer the question>}'
    )


def _try_parse(raw: str) -> Answer | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("qa: LLM response was not valid JSON: %s", exc)
        return None
    try:
        return Answer.model_validate(data)
    except ValidationError as exc:
        logger.warning("qa: LLM JSON didn't match the Answer schema: %s", exc)
        return None


def _call_groq(prompt: str) -> str:
    from groq import Groq  # local import: no SDK cost on any path that never calls an LLM

    client = Groq(api_key=get_settings().groq_api_key)
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return completion.choices[0].message.content


def _call_gemini(prompt: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=get_settings().gemini_api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Answer,
        ),
    )
    return response.text


def _providers() -> list[tuple[str, Callable[[str], str]]]:
    """Groq primary, Gemini fallback. A class hierarchy would be overkill
    for a v0 with one retry and one fallback; per-provider budget
    tracking/backoff is future work if this needs to get smarter."""
    return [("groq", _call_groq), ("gemini", _call_gemini)]


def _generate(prompt: str) -> Answer | None:
    """Try each provider in order; retry once per provider on a parse
    failure before moving to the next. A provider call that raises
    (network/auth/rate-limit) is NOT retried — it moves straight to the
    next provider instead. Returns None only if every provider has either
    failed to respond or never produced parseable output; the caller turns
    that into insufficient_evidence, never a crash.
    """
    for name, call_fn in _providers():
        for attempt in (1, 2):
            try:
                raw = call_fn(prompt)
            except Exception as exc:  # noqa: BLE001 — any provider failure falls through to the next provider
                logger.warning("qa: %s call failed: %s", name, exc)
                break
            answer = _try_parse(raw)
            if answer is not None:
                return answer
            logger.warning("qa: %s produced unparseable output (attempt %d/2)", name, attempt)
    return None


def answer_question(
    query: str,
    *,
    ticker: str | None = None,
    form_type: str | None = None,
    since: date | None = None,
) -> Answer:
    """Retrieve, score-gate, and (if warranted) generate a cited answer."""
    results = search(query, ticker=ticker, form_type=form_type, since=since)  # spec top-40

    if not results or results[0].fusion_score < SCORE_FLOOR:
        return _insufficient_evidence()

    context_results = rerank(query, results, top_k=LLM_CONTEXT_TOP_N)
    valid_chunk_ids = {r.chunk_id for r in context_results}

    raw_answer = _generate(_build_prompt(query, context_results))
    if raw_answer is None:
        return _insufficient_evidence()

    # Never trust a citation to a chunk_id the model wasn't shown: drop
    # hallucinated ids from each claim, and drop any claim left with none
    # at all (an uncited claim isn't something we can stand behind).
    filtered_claims = []
    for claim in raw_answer.claims:
        kept_ids = [cid for cid in claim.chunk_ids if cid in valid_chunk_ids]
        if kept_ids:
            filtered_claims.append(Claim(text=claim.text, chunk_ids=kept_ids))

    return Answer(
        answer=raw_answer.answer,
        claims=filtered_claims,
        confidence=raw_answer.confidence,
        insufficient_evidence=raw_answer.insufficient_evidence,
    )
