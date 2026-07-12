"""Parser tests: HTML -> lines -> sections -> silver Parquet.

Fixtures are synthetic but structurally faithful to real EDGAR markup
(Item headings split across adjacent spans/divs, Part I/II item-number
collisions) — the exact ambiguities that break naive parsers.
"""

from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from filingsage.connectors.models import FilingRef
from filingsage.parsing.html import html_to_lines
from filingsage.parsing.sections import split_sections
from filingsage.parsing.silver import ParseQuarantineError, parse_to_silver

FIXTURES = Path(__file__).parent / "fixtures"


def _ref(form_type: str, accession: str = "0000320193-26-000013") -> FilingRef:
    return FilingRef(
        cik=320193, ticker="AAPL", company="Apple Inc.",
        accession_number=accession, form_type=form_type,
        filed_at=date(2026, 5, 1), primary_document="doc.htm",
    )


def test_html_to_lines_splits_adjacent_spans():
    html = "<div><span>Item 1A.</span><span>Risk Factors</span></div>"
    lines = html_to_lines(html)
    assert lines == ["Item 1A.", "Risk Factors"]


def test_10q_part_i_and_ii_item_1_do_not_collide():
    lines = html_to_lines((FIXTURES / "sample_10q.htm").read_text())
    sections = split_sections(lines, "10-Q")
    keys = [s.key for s in sections]
    assert "financial_statements" in keys   # Part I, Item 1
    assert "legal_proceedings" in keys       # Part II, Item 1
    assert keys.count("financial_statements") == 1  # not merged/duplicated


def test_10q_all_expected_sections_found():
    lines = html_to_lines((FIXTURES / "sample_10q.htm").read_text())
    sections = split_sections(lines, "10-Q")
    assert {s.key for s in sections} == {
        "financial_statements", "mdna", "market_risk",
        "controls_and_procedures", "legal_proceedings", "risk_factors",
    }


def test_8k_dotted_item_numbers_parsed():
    lines = html_to_lines((FIXTURES / "sample_8k.htm").read_text())
    sections = split_sections(lines, "8-K")
    assert {s.item_no for s in sections} == {"2.02", "9.01"}
    assert {s.key for s in sections} == {"results_of_operations", "financial_statements_exhibits"}


def test_unrecognized_form_type_returns_empty():
    assert split_sections(["Item 1.", "Something"], "S-1") == []


def test_8k_single_line_heading_with_period():
    # Real pattern from NVDA 8-K 0001045810-26-000060 (nvda-20260628.htm),
    # line 62 — item number, period, and full official title all on one
    # rendered line, no separate heading span.
    lines = [
        "Item 5.02. Departure of Directors or Certain Officers; Election of "
        "Directors; Appointment of Certain Officers; Compensatory Arrangements "
        "of Certain Officers.",
        "Ajay K. Puri notified the Company of his intention to retire.",
    ]
    sections = split_sections(lines, "8-K")
    assert len(sections) == 1
    assert sections[0].key == "officer_changes"
    assert sections[0].item_no == "5.02"
    assert sections[0].heading.startswith("Departure of Directors")
    assert "Ajay K. Puri" in sections[0].text


def test_8k_single_line_heading_no_period_multiple_spaces():
    # Real pattern from AAPL 8-K 0000320193-26-000011 (aapl-20260430.htm),
    # line 105 — no trailing period, multiple spaces before the heading.
    lines = [
        "Item 2.02    Results of Operations and Financial Condition.",
        "On April 30, 2026, the Company issued a press release.",
    ]
    sections = split_sections(lines, "8-K")
    assert len(sections) == 1
    assert sections[0].key == "results_of_operations"
    assert sections[0].heading == "Results of Operations and Financial Condition."
    assert "press release" in sections[0].text


def test_8k_two_line_heading_with_thin_space_still_works():
    # Real pattern from MSFT 8-K 0001193125-26-224155 (d125909d8k.htm),
    # lines 93/102 — item marker alone on its own line, using a Unicode
    # thin space ( ) between "Item" and the number, heading on the
    # following line. Protects the pre-existing two-line fallback.
    lines = [
        "Item 5.02.",
        "Departure of Directors or Certain Officers",
        "Body text about the departure.",
    ]
    sections = split_sections(lines, "8-K")
    assert len(sections) == 1
    assert sections[0].key == "officer_changes"
    assert sections[0].heading == "Departure of Directors or Certain Officers"
    assert "Body text" in sections[0].text


def test_8k_unmapped_item_passes_through_instead_of_quarantining():
    # Real pattern: an 8-K whose only item isn't in the curated FORM_8K_ITEMS
    # set. Quarantine must mean "could not parse", never "chose not to
    # track" — so this becomes a generically-keyed section, not zero sections.
    lines = [
        "Item 3.01. Notice of Delisting or Failure to Satisfy a Continued "
        "Listing Rule or Standard; Transfer of Listing.",
        "On June 1, 2026, the Company received a notice from the exchange.",
    ]
    sections = split_sections(lines, "8-K")
    assert len(sections) == 1
    assert sections[0].key == "item_3_01"
    assert sections[0].item_no == "3.01"
    assert sections[0].heading.startswith("Notice of Delisting")
    assert "received a notice" in sections[0].text


def test_8k_named_item_5_07_still_maps_to_shareholder_votes():
    lines = [
        "Item 5.07. Submission of Matters to a Vote of Security Holders.",
        "Stockholders approved the election of directors.",
    ]
    sections = split_sections(lines, "8-K")
    assert len(sections) == 1
    assert sections[0].key == "shareholder_votes"
    assert sections[0].item_no == "5.07"


def test_8k_prose_starting_with_item_number_is_not_a_marker():
    # Negative case: a paragraph that happens to start with "Item 5.02" but
    # is regulatory prose, not a heading. Trailing text exceeds
    # _MAX_INLINE_HEADING_LEN (200 chars) so it must not be treated as a
    # section marker.
    prose = (
        "Item 5.02 of Form 8-K requires the registrant to disclose any "
        "departure of directors or principal officers from the registrant, "
        "including departures resulting from retirement, resignation or "
        "removal, as well as the election or appointment of new directors "
        "or officers and any material changes to compensatory arrangements "
        "in connection therewith."
    )
    assert len(prose) - len("Item 5.02 ") > 200
    lines = [prose, "Some unrelated following paragraph."]
    assert split_sections(lines, "8-K") == []


def test_parse_to_silver_writes_valid_parquet(tmp_path):
    ref = _ref("10-Q")
    result = parse_to_silver(FIXTURES / "sample_10q.htm", ref, tmp_path)

    assert result.section_count == 6
    assert result.duplicate_count == 0
    assert result.silver_path.exists()
    assert not list(result.silver_path.parent.glob("*.tmp"))  # atomic write

    table = pq.read_table(result.silver_path)
    assert table.num_rows == 6
    rows = table.to_pylist()
    assert all(r["accession_number"] == ref.accession_number for r in rows)
    assert all(r["char_count"] == len(r["text"]) for r in rows)
    assert all(len(r["text_hash"]) == 64 for r in rows)  # sha256 hex digest


def test_parse_to_silver_quarantines_unparseable_content(tmp_path):
    bad = tmp_path / "bad.htm"
    bad.write_text("<html><body><p>No SEC Item headings anywhere in here.</p></body></html>")
    with pytest.raises(ParseQuarantineError, match="no sections"):
        parse_to_silver(bad, _ref("10-Q"), tmp_path / "silver")
    # quarantine must never leave a partial silver file behind
    assert not list((tmp_path / "silver").rglob("*.parquet"))


def test_parse_to_silver_quarantines_bad_encoding(tmp_path):
    bad = tmp_path / "bad.htm"
    bad.write_bytes(b"\xff\xfe\x00broken")
    with pytest.raises(ParseQuarantineError, match="encoding"):
        parse_to_silver(bad, _ref("10-Q"), tmp_path / "silver")


def test_dedupe_drops_exact_duplicate_sections(tmp_path):
    html = """
    <div><span>Item 1.</span><span>Financial Statements</span></div>
    <p>This exact boilerplate paragraph appears twice in the filing for testing.</p>
    <div><span>Item 2.</span><span>MD&A</span></div>
    <p>This exact boilerplate paragraph appears twice in the filing for testing.</p>
    """
    dup_file = tmp_path / "dup.htm"
    dup_file.write_text(html)
    result = parse_to_silver(dup_file, _ref("10-Q"), tmp_path / "silver")
    assert result.duplicate_count == 1
    assert result.section_count == 1


def test_silver_parquet_readable_by_duckdb(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    ref = _ref("10-Q")
    result = parse_to_silver(FIXTURES / "sample_10q.htm", ref, tmp_path)
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT section, char_count FROM '{result.silver_path}' ORDER BY seq"
    ).fetchall()
    assert len(rows) == 6
    assert rows[0][0] == "financial_statements"