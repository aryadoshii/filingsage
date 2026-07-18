"""Gold-layer chunking tests.

count_tokens/_split_by_tokens use the real BAAI/bge-small-en-v1.5 tokenizer
(via `tokenizers.Tokenizer.from_pretrained`) rather than a mock — the whole
point of matching FastEmbed's tokenizer is that boundaries are real token
boundaries, so tests assert against real tokenization, same philosophy as
using real Postgres via testcontainers elsewhere in this suite. First run in
a fresh environment fetches ~1MB from the HF Hub and caches it locally;
subsequent runs are offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from filingsage.connectors.models import FilingRef
from filingsage.db.models import Chunk as ChunkRow
from filingsage.db.models import Company, Filing
from filingsage.gold.chunking import (
    CHUNK_OVERLAP_TOKENS,
    MAX_CHUNK_TOKENS,
    GoldChunk,
    _tokenizer,
    chunk_filing,
    chunk_sections,
    count_tokens,
    persist_chunks,
)
from filingsage.parsing.sections import Section
from filingsage.parsing.silver import parse_to_silver

FIXTURES = Path(__file__).parent / "fixtures"

_LONG_SENTENCE = (
    "The Company faces macroeconomic headwinds including inflation, elevated "
    "interest rates, foreign currency fluctuations, supply chain disruption, "
    "and heightened competition across its core markets and product lines. "
)


def _long_section(key: str = "risk_factors", item_no: str = "1A") -> Section:
    # ~35-40 tokens per repeat; 80 repeats comfortably clears MAX_CHUNK_TOKENS
    # (384) several times over, forcing multiple overlapping windows.
    return Section(key=key, item_no=item_no, heading="Risk Factors", text=_LONG_SENTENCE * 80)


def _short_section(key: str, item_no: str, text: str) -> Section:
    return Section(key=key, item_no=item_no, heading=key, text=text)


def _overlap_len(a_ids: list[int], b_ids: list[int]) -> int:
    """Length of the longest suffix of a_ids that's also a prefix of b_ids."""
    for k in range(min(len(a_ids), len(b_ids)), 0, -1):
        if a_ids[-k:] == b_ids[:k]:
            return k
    return 0


def test_long_section_splits_with_correct_token_overlap():
    section = _long_section()
    chunks = chunk_sections([section])

    assert len(chunks) > 1

    tok = _tokenizer()
    for prev, nxt in zip(chunks, chunks[1:]):
        prev_ids = tok.encode(prev.text, add_special_tokens=False).ids
        next_ids = tok.encode(nxt.text, add_special_tokens=False).ids
        # >=, not ==: boundaries snap outward to whole-word edges (see
        # chunking._split_by_tokens), so overlap can round up past the
        # configured minimum by a word's worth of tokens — it must never
        # round down below it.
        assert _overlap_len(prev_ids, next_ids) >= CHUNK_OVERLAP_TOKENS


def test_short_section_becomes_exactly_one_chunk():
    text = "The Company's total net sales increased 5% during the quarter."
    section = _short_section("mdna", "7", text)

    chunks = chunk_sections([section])

    assert len(chunks) == 1
    assert chunks[0].text == text  # exact substring, no reconstruction artifacts
    assert chunks[0].token_count == count_tokens(text)


def test_two_sections_never_share_a_chunk():
    section_a = _short_section("business", "1", "Example Corp designs and sells consumer electronics.")
    section_b = _short_section("mdna", "7", "Net sales increased 5% year over year in the third quarter.")

    chunks = chunk_sections([section_a, section_b])

    assert len(chunks) == 2
    assert chunks[0].section == "business"
    assert chunks[1].section == "mdna"
    assert "Net sales" not in chunks[0].text
    assert "consumer electronics" not in chunks[1].text


def test_token_count_never_exceeds_configured_max():
    chunks = chunk_sections([_long_section()])
    assert chunks  # sanity: the long section actually produced chunks
    assert all(c.token_count <= MAX_CHUNK_TOKENS for c in chunks)


def test_text_hash_dedup_drops_identical_chunks():
    duplicate_text = "This exact boilerplate paragraph appears twice in the filing."
    section_a = _short_section("business", "1", duplicate_text)
    section_b = _short_section("legal_proceedings", "3", duplicate_text)  # same text, different section
    section_c = _short_section("mdna", "7", "This paragraph is genuinely different from the others.")

    chunks = chunk_sections([section_a, section_b, section_c])

    # section_b's chunk is dropped as a duplicate of section_a's; section_c survives.
    assert [c.section for c in chunks] == ["business", "mdna"]


def test_seq_values_are_contiguous_and_unique_after_dedup():
    duplicate_text = "This exact boilerplate paragraph appears twice in the filing."
    section_a = _short_section("business", "1", duplicate_text)
    section_b = _short_section("legal_proceedings", "3", duplicate_text)  # dropped as a duplicate
    section_c = _short_section("mdna", "7", "This paragraph is genuinely different from the others.")

    chunks = chunk_sections([section_a, section_b, section_c])

    seqs = [c.seq for c in chunks]
    assert seqs == list(range(len(chunks)))  # contiguous, no gap where section_b was dropped
    assert len(set(seqs)) == len(seqs)  # unique


def test_chunk_filing_reads_silver_parquet_one_chunk_per_section(tmp_path):
    ref = FilingRef(
        cik=320193, ticker="AAPL", company="Apple Inc.",
        accession_number="0000320193-26-000013", form_type="10-Q",
        filed_at=date(2026, 5, 1), primary_document="doc.htm",
    )
    result = parse_to_silver(FIXTURES / "sample_10q.htm", ref, tmp_path)

    chunks = chunk_filing(result.silver_path)

    assert {c.section for c in chunks} == {
        "financial_statements", "mdna", "market_risk",
        "controls_and_procedures", "legal_proceedings", "risk_factors",
    }
    assert all(isinstance(c, GoldChunk) for c in chunks)
    assert all(c.token_count <= MAX_CHUNK_TOKENS for c in chunks)


# --- persistence (testcontainers Postgres, real alembic migrations) --------


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield create_engine(url)


@pytest.mark.integration
def test_persist_chunks_is_idempotent(engine):
    with Session(engine) as session:
        session.add(Company(cik=1111111, ticker="CHNK", name="Chunk Test Co"))
        filing = Filing(
            cik=1111111, accession_no="chunk-test-0001", form_type="8-K",
            filed_at=date(2026, 1, 1), primary_document="a.htm",
        )
        session.add(filing)
        session.flush()
        filing_id = filing.id

        chunks = [
            GoldChunk(section="other_events", item_no="8.01", seq=0, text="First chunk.",
                      text_hash="hash-0", char_count=12, token_count=3),
            GoldChunk(section="other_events", item_no="8.01", seq=1, text="Second chunk.",
                      text_hash="hash-1", char_count=13, token_count=3),
        ]

        first_run = persist_chunks(session, filing_id, chunks)
        session.commit()
        assert first_run == 2

        second_run = persist_chunks(session, filing_id, chunks)  # re-chunking: no-op
        session.commit()
        assert second_run == 0

        total = session.scalar(
            select(func.count()).select_from(ChunkRow).where(ChunkRow.filing_id == filing_id)
        )
        assert total == 2
