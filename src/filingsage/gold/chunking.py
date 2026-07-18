"""Section-aware chunking: silver Parquet rows -> ~512-token chunks with overlap.

Gold layer, spec §5/§6: this is the first of two increments — chunking here,
embeddings + Qdrant upsert in the next one. Deliberately not wired into the
Celery chain yet (see filingsage.worker.tasks) so this stays callable and
testable in isolation before anything downstream depends on it.

Token counting uses the SAME tokenizer FastEmbed's dense model
(BAAI/bge-small-en-v1.5) uses internally, so a chunk's token_count matches
what actually gets fed to the embedding model at embed time (next
increment) — nothing silently truncates because we counted with a
different notion of "token" than the one that matters.

FastEmbed's TextEmbedding class doesn't expose a way to fetch just the
tokenizer without also downloading the full ONNX model weights (100MB+) at
construction — the two are loaded together internally, and until the next
increment we don't need the weights at all. Instead we load the tokenizer
directly via the `tokenizers` library (already a fastembed dependency)
using the same `BAAI/bge-small-en-v1.5` tokenizer.json FastEmbed itself
reads — same file, byte-identical tokenization, ~1MB fetched and cached
locally instead of ~130MB. `count_tokens` is the single seam this all runs
through, so swapping the embedding model later means changing one function.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tokenizers import Tokenizer

from filingsage.db.models import Chunk as ChunkRow
from filingsage.parsing.sections import Section

# Must match the dense embedding model used in the next increment — this is
# what makes count_tokens() meaningful rather than an arbitrary estimate.
TOKENIZER_MODEL = "BAAI/bge-small-en-v1.5"

# BGE-small-en-v1.5's real context window is 512 tokens INCLUDING the
# [CLS]/[SEP] special tokens FastEmbed adds at actual embed time. We budget
# 510 content tokens here (2 reserved for those specials) so a chunk plus
# its special tokens never exceeds the model's true limit — "~512" per the
# task, with the 2-token reservation spelled out rather than silently
# eating into headroom no one asked to give up.
MAX_CHUNK_TOKENS = 510

# ~12.5% of MAX_CHUNK_TOKENS. Enough shared context that a fact split across
# a chunk boundary still appears whole in at least one chunk, without
# ballooning the number of chunks (and embedding calls) per filing.
CHUNK_OVERLAP_TOKENS = 64


@lru_cache(maxsize=1)
def _tokenizer() -> Tokenizer:
    return Tokenizer.from_pretrained(TOKENIZER_MODEL)


def count_tokens(text: str) -> int:
    """Token count under the tokenizer the embedding model will actually use.

    Excludes special tokens ([CLS]/[SEP]) — this counts chunk CONTENT, the
    same quantity MAX_CHUNK_TOKENS budgets against.
    """
    return len(_tokenizer().encode(text, add_special_tokens=False).ids)


@dataclass(frozen=True, slots=True)
class GoldChunk:
    section: str
    item_no: str
    seq: int
    text: str
    text_hash: str
    char_count: int
    token_count: int


def _split_by_tokens(text: str, max_tokens: int, overlap_tokens: int) -> list[tuple[str, int]]:
    """Split text into (chunk_text, token_count) windows of <= max_tokens.

    Splits land on WHOLE-WORD boundaries, not just token boundaries. A
    WordPiece tokenizer marks continuation subwords with "##" (e.g. "head",
    "##wind", "##s" for "headwinds") — that marker only makes sense next to
    the piece before it. If a window boundary fell mid-word, the trailing
    half re-tokenized in isolation (no preceding piece to continue) would
    produce a DIFFERENT token id than it had in context, silently
    invalidating the token_count we computed here versus what FastEmbed
    actually produces when it tokenizes the chunk's text fresh at embed
    time. `Encoding.word_ids` gives each token's word index, so boundaries
    only land where the word index changes.

    Chunk text is always sliced from the ORIGINAL string by character
    offset — never reconstructed by decoding token ids — so it's an exact
    substring of the source, with no tokenizer round-trip artifacts.
    """
    encoding = _tokenizer().encode(text, add_special_tokens=False)
    ids, offsets, word_ids = encoding.ids, encoding.offsets, encoding.word_ids
    n = len(ids)
    if n <= max_tokens:
        return [(text, n)]

    word_starts = [i for i in range(n) if i == 0 or word_ids[i] != word_ids[i - 1]]

    def boundary_at_or_before(idx: int) -> int:
        result = word_starts[0]
        for b in word_starts:
            if b > idx:
                break
            result = b
        return result

    def boundary_after(idx: int) -> int:
        for b in word_starts:
            if b > idx:
                return b
        return n

    windows: list[tuple[str, int]] = []
    start = 0
    while start < n:
        target_end = start + max_tokens
        end = boundary_at_or_before(target_end) if target_end < n else n
        if end <= start:
            # A single word's subword pieces alone exceed max_tokens (never
            # seen in real filing prose, but a hostile/degenerate input
            # shouldn't infinite-loop or split a word) — take that one word
            # whole rather than fabricate a mid-word boundary.
            end = boundary_after(start)
        piece = text[offsets[start][0] : offsets[end - 1][1]]
        windows.append((piece, end - start))
        if end >= n:
            break
        target_start = end - overlap_tokens
        next_start = boundary_at_or_before(target_start) if target_start > start else end
        start = next_start
    return windows


def chunk_sections(sections: list[Section]) -> list[GoldChunk]:
    """Chunk a filing's sections, in order. Never merges across sections —
    each section is windowed independently, so a chunk boundary is always
    either inside one section or exactly at a section's edge.

    Identical chunks (by text_hash) are dropped within the filing, same
    reasoning as the silver-layer section dedupe (spec: boilerplate
    sometimes repeats verbatim). seq is assigned only to KEPT chunks, so it
    stays contiguous even when duplicates are dropped.
    """
    chunks: list[GoldChunk] = []
    seen_hashes: set[str] = set()
    seq = 0
    for section in sections:
        for piece, token_count in _split_by_tokens(
            section.text, MAX_CHUNK_TOKENS, CHUNK_OVERLAP_TOKENS
        ):
            text_hash = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            if text_hash in seen_hashes:
                continue
            seen_hashes.add(text_hash)
            chunks.append(
                GoldChunk(
                    section=section.key,
                    item_no=section.item_no,
                    seq=seq,
                    text=piece,
                    text_hash=text_hash,
                    char_count=len(piece),
                    token_count=token_count,
                )
            )
            seq += 1
    return chunks


def chunk_filing(silver_path: Path) -> list[GoldChunk]:
    """Read a filing's silver Parquet (one row per section) and chunk it."""
    table = pq.read_table(silver_path, columns=["section", "item_no", "heading", "text"])
    sections = [
        Section(key=row["section"], item_no=row["item_no"], heading=row["heading"], text=row["text"])
        for row in table.to_pylist()
    ]
    return chunk_sections(sections)


def persist_chunks(session: Session, filing_id: int, chunks: list[GoldChunk]) -> int:
    """Insert chunks for a filing, idempotently.

    ON CONFLICT (filing_id, seq) DO NOTHING: re-chunking an already-chunked
    filing is a no-op, consistent with the rest of the pipeline (fetch,
    parse). Returns the number of rows actually inserted.

    Persists only the columns spec §4 defines for `chunks` — item_no is
    part of GoldChunk (useful in-process, e.g. for citation display) but
    isn't a `chunks` column, so it's dropped here rather than persisted.
    """
    inserted = 0
    for chunk in chunks:
        result = session.execute(
            pg_insert(ChunkRow)
            .values(
                filing_id=filing_id,
                section=chunk.section,
                seq=chunk.seq,
                text=chunk.text,
                text_hash=chunk.text_hash,
                char_count=chunk.char_count,
                token_count=chunk.token_count,
            )
            .on_conflict_do_nothing(index_elements=["filing_id", "seq"])
            .returning(ChunkRow.id)
        )
        if result.first() is not None:
            inserted += 1
    return inserted
