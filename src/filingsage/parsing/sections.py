"""Section detection for 10-K/10-Q/8-K filings (spec §5, silver layer).

SEC filings aren't uniformly marked up — no filer-agnostic tag or class
identifies an "Item" header. What IS consistent, because it's mandated by
Regulation S-K, is the wording: every 10-K/10-Q partitions into numbered
Items ("Item 1A. Risk Factors", "Item 7. MD&A", ...) and every 8-K into
numbered Items from Form 8-K's fixed catalog ("Item 2.02", "Item 9.01", ...).
So we detect sections by TEXT PATTERN on the linearized document, not by
HTML structure — the one signal that survives filer-to-filer markup chaos.

8-K item coverage: every 8-K item is a material event by definition (that's
why it triggered a filing at all), and the document's own heading supplies
the official item title — so for 8-Ks a validly-matched item number outside
FORM_8K_ITEMS still becomes a section, under a generic f"item_{no}" key,
rather than being dropped. Quarantine must mean "could not parse", never
"chose not to track": the quarantine rate is a public quality metric, and
mixing "parse failure" with "out of scope" would make that metric dishonest.
10-K/10-Q keep the curated-map skip behavior — their catalogs include genuine
boilerplate items (exhibits, signatures) excluded on purpose, and both forms
always contain at least one tracked item, so they can't spuriously quarantine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 10-K/10-Q items we care about for v1 (spec: Item 1A Risk Factors, Item 7 MD&A,
# "etc." — we take the commonly-cited high-value set, not the full catalog).
# Maps normalized item number -> canonical section name.
FORM_10_ITEMS: dict[str, str] = {
    "1": "business",
    "1a": "risk_factors",
    "2": "properties",
    "3": "legal_proceedings",
    "7": "mdna",
    "7a": "market_risk",
    "8": "financial_statements",
}

# 10-Q Part I and Part II both start numbering at Item 1 — same item number,
# different meaning ("Item 1" = Financial Statements in Part I, Legal
# Proceedings in Part II). Keyed by (part, item_no).
FORM_10Q_ITEMS: dict[tuple[int, str], str] = {
    (1, "1"): "financial_statements",
    (1, "2"): "mdna",
    (1, "3"): "market_risk",
    (1, "4"): "controls_and_procedures",
    (2, "1"): "legal_proceedings",
    (2, "1a"): "risk_factors",
}
FORM_8K_ITEMS: dict[str, str] = {
    "1.01": "material_agreement",
    "2.02": "results_of_operations",
    "5.02": "officer_changes",
    "5.07": "shareholder_votes",
    "7.01": "regulation_fd",
    "8.01": "other_events",
    "9.01": "financial_statements_exhibits",
}

# "Item 1A." / "Item 7." / "Item 2.02" / "ITEM 1A" — SEC filings vary case,
# punctuation, and whether a trailing period is present. Some filers also put
# the heading text on the SAME line as the item number ("Item 5.02. Departure
# of Directors..."), rather than in a separate span/line — group(2) captures
# that trailing text, if any, so split_sections can use it as the heading.
_ITEM_RE = re.compile(
    r"^\s*item\s+(\d{1,2}[a-z]?(?:\.\d{2})?)\.?\s*(.*)$",
    re.IGNORECASE,
)
# Longest official Item title text we expect on a heading line. Item 5.02's
# full title ("Departure of Directors or Certain Officers; Election of
# Directors; Appointment of Certain Officers; Compensatory Arrangements of
# Certain Officers.") runs ~150 chars — the longest of the FORM_8K_ITEMS/
# FORM_10_ITEMS titles we track. A trailing capture longer than this is prose
# that merely starts with "Item N" (e.g. "Item 5.02 of Form 8-K requires..."),
# not a heading, so the line isn't treated as a marker at all.
_MAX_INLINE_HEADING_LEN = 200
_PART_RE = re.compile(r"^\s*part\s+(i{1,3}|iv)\b", re.IGNORECASE)
_ROMAN_TO_INT = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


@dataclass(frozen=True, slots=True)
class Section:
    key: str          # canonical name, e.g. "risk_factors"
    item_no: str       # as detected, e.g. "1A"
    heading: str        # heading text as it appeared, e.g. "Risk Factors"
    text: str


def _item_map_for(form_type: str) -> dict:
    if form_type == "10-K":
        return FORM_10_ITEMS
    if form_type == "10-Q":
        return FORM_10Q_ITEMS
    if form_type == "8-K":
        return FORM_8K_ITEMS
    return {}


def split_sections(lines: list[str], form_type: str) -> list[Section]:
    """Split a filing's text lines into Items, by SEC-mandated heading text.

    Two-line heading pattern handled: EDGAR's XBRL viewer commonly renders
    "Item 1A." and "Risk Factors" as separate lines/spans (see fixtures) —
    so when a line matches _ITEM_RE, the following non-empty line is treated
    as the heading text if it's short (a heading, not a paragraph).
    """
    item_map = _item_map_for(form_type)
    if not item_map:
        return []
    is_10q = form_type == "10-Q"
    is_8k = form_type == "8-K"

    # Walk once, tracking the current "PART" (10-Qs only) so Item 1 in Part I
    # and Item 1 in Part II resolve to different sections. Non-10-Q forms
    # (10-K, 8-K) don't use PART headers this way, so part stays fixed at 1.
    # (line_idx, item_no, part, inline_heading) — inline_heading is the
    # trailing text captured on the marker line itself, or None when the
    # heading (if any) is on the following line instead.
    markers: list[tuple[int, str, int, str | None]] = []
    current_part = 1
    for i, line in enumerate(lines):
        if is_10q:
            pm = _PART_RE.match(line)
            if pm:
                current_part = _ROMAN_TO_INT.get(pm.group(1).lower(), current_part)
                continue
        m = _ITEM_RE.match(line)
        if m:
            trailing = m.group(2).strip()
            if len(trailing) > _MAX_INLINE_HEADING_LEN:
                continue  # prose that merely starts with "Item N", not a marker
            markers.append((i, m.group(1).lower(), current_part, trailing or None))

    sections: list[Section] = []
    for idx, (start, item_no, part, inline_heading) in enumerate(markers):
        lookup_key = (part, item_no) if is_10q else item_no
        key = item_map.get(lookup_key)
        if key is None:
            if not is_8k:
                continue  # 10-K/10-Q: an Item outside our tracked set — skip, don't error
            key = f"item_{item_no.replace('.', '_')}"  # 8-K: full passthrough, never skip

        heading = ""
        body_start = start + 1
        if inline_heading is not None:
            heading = inline_heading
        elif body_start < len(lines) and 0 < len(lines[body_start]) <= 120:
            heading = lines[body_start]
            body_start += 1

        end = markers[idx + 1][0] if idx + 1 < len(markers) else len(lines)
        body = "\n".join(l for l in lines[body_start:end] if l.strip())

        sections.append(
            Section(key=key, item_no=item_no.upper(), heading=heading, text=body)
        )
    return sections