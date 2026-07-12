"""Bronze -> silver: parse, section, quality-check, write typed Parquet.

Spec §5: "Data-quality checks here (non-empty sections, encoding, dedupe by
text hash) — failures quarantine the filing and emit filing.parse_failed."
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from filingsage.connectors.models import FilingRef
from filingsage.parsing.html import html_to_lines
from filingsage.parsing.sections import Section, split_sections

MIN_SECTION_CHARS = 50  # below this, a "section" is a stray heading, not content

_SCHEMA = pa.schema(
    [
        pa.field("cik", pa.int64()),
        pa.field("ticker", pa.string()),
        pa.field("accession_number", pa.string()),
        pa.field("form_type", pa.string()),
        pa.field("filed_at", pa.date32()),
        pa.field("section", pa.string()),
        pa.field("item_no", pa.string()),
        pa.field("heading", pa.string()),
        pa.field("seq", pa.int32()),
        pa.field("text", pa.string()),
        pa.field("text_hash", pa.string()),
        pa.field("char_count", pa.int32()),
    ]
)


class ParseQuarantineError(Exception):
    """Raised when a filing fails data-quality checks (spec §5).

    Caller (Celery task, later increment) catches this, marks the filing
    QUARANTINED, and emits filing.parse_failed with the reason.
    """


@dataclass(frozen=True, slots=True)
class ParseResult:
    silver_path: Path
    section_count: int
    duplicate_count: int  # sections dropped as exact duplicates of an earlier one


def _dedupe(sections: list[Section]) -> tuple[list[Section], int]:
    """Drop exact-duplicate sections by content hash (spec: "dedupe by text hash").

    Boilerplate legal sections are sometimes repeated verbatim within a
    filing (e.g. quoted in both a summary and a full section); keeping only
    the first occurrence avoids double-counting in retrieval later.
    """
    seen: set[str] = set()
    kept: list[Section] = []
    dropped = 0
    for s in sections:
        h = hashlib.sha256(s.text.encode("utf-8")).hexdigest()
        if h in seen:
            dropped += 1
            continue
        seen.add(h)
        kept.append(s)
    return kept, dropped


def parse_to_silver(bronze_path: Path, ref: FilingRef, silver_dir: Path) -> ParseResult:
    """Parse one bronze document into a silver Parquet file.

    Raises ParseQuarantineError (never writes partial output) if:
      - the document can't be decoded as text at all
      - zero recognized sections are found
      - every found section is empty/near-empty after whitespace stripping
    """
    try:
        html = bronze_path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ParseQuarantineError(f"encoding error reading {bronze_path.name}: {exc}") from exc

    lines = html_to_lines(html)
    sections = split_sections(lines, ref.form_type)
    sections = [s for s in sections if len(s.text) >= MIN_SECTION_CHARS]

    if not sections:
        raise ParseQuarantineError(
            f"no sections >= {MIN_SECTION_CHARS} chars found for "
            f"{ref.form_type} {ref.accession_number} (bronze may be malformed, "
            f"or this filer's markup doesn't match our Item-heading pattern)"
        )

    sections, duplicate_count = _dedupe(sections)

    rows = {
        "cik": [], "ticker": [], "accession_number": [], "form_type": [], "filed_at": [],
        "section": [], "item_no": [], "heading": [], "seq": [], "text": [],
        "text_hash": [], "char_count": [],
    }
    for seq, s in enumerate(sections):
        rows["cik"].append(ref.cik)
        rows["ticker"].append(ref.ticker)
        rows["accession_number"].append(ref.accession_number)
        rows["form_type"].append(ref.form_type)
        rows["filed_at"].append(ref.filed_at)
        rows["section"].append(s.key)
        rows["item_no"].append(s.item_no)
        rows["heading"].append(s.heading)
        rows["seq"].append(seq)
        rows["text"].append(s.text)
        rows["text_hash"].append(hashlib.sha256(s.text.encode("utf-8")).hexdigest())
        rows["char_count"].append(len(s.text))

    table = pa.table(rows, schema=_SCHEMA)

    dest = silver_dir / "filings" / f"{ref.accession_number}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp)
    tmp.replace(dest)  # atomic, same reasoning as bronze fetch

    return ParseResult(silver_path=dest, section_count=len(sections), duplicate_count=duplicate_count)