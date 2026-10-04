"""Pure display helpers — no Streamlit, no network, so tests can cover them
directly. Everything a user reads is named here in plain language, not in
the pipeline's internal vocabulary."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime

# Parser section keys (parsing/sections.py) -> what a reader calls them.
SECTION_LABELS: dict[str, str] = {
    "business": "Business",
    "risk_factors": "Risk factors",
    "properties": "Properties",
    "legal_proceedings": "Legal proceedings",
    "mdna": "Management's discussion (MD&A)",
    "market_risk": "Market risk",
    "financial_statements": "Financial statements",
    "controls_and_procedures": "Controls and procedures",
    "material_agreement": "Material agreement",
    "results_of_operations": "Results of operations",
    "officer_changes": "Officer and director changes",
    "shareholder_votes": "Shareholder votes",
    "regulation_fd": "Reg FD disclosure",
    "other_events": "Other events",
    "financial_statements_exhibits": "Financial statements and exhibits",
}

# Pipeline status -> (label a user understands, tone). Tone picks a reserved
# status color in the UI and always ships with the label, never color alone.
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "discovered": ("Queued", "pending"),
    "fetched": ("Downloaded", "pending"),
    "parsed": ("Parsed", "pending"),
    "embedded": ("Searchable", "good"),
    "quarantined": ("Couldn't parse", "bad"),
}

IN_PROGRESS_STATUSES = ("discovered", "fetched", "parsed")

FORM_DESCRIPTIONS: dict[str, str] = {
    "10-K": "Annual report",
    "10-Q": "Quarterly report",
    "8-K": "Current report",
}

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def section_label(key: str) -> str:
    if key in SECTION_LABELS:
        return SECTION_LABELS[key]
    match = re.fullmatch(r"item_(.+)", key)
    if match:
        return f"Item {match.group(1).upper()}"
    return key.replace("_", " ").capitalize()


def status_label(status: str) -> tuple[str, str]:
    return STATUS_LABELS.get(status, (status.capitalize(), "pending"))


def format_date(value: str | date | None) -> str:
    """'2025-10-31' -> '31 Oct 2025'. Unambiguous across locales."""
    if value is None:
        return "—"
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{d.day} {d.strftime('%b %Y')}"


def relative_time(value: str | datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    ts = datetime.fromisoformat(value) if isinstance(value, str) else value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    seconds = int(((now or datetime.now(UTC)) - ts).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def normalize_ticker(raw: str) -> str | None:
    """Upper-cased ticker if it looks like one, else None. EDGAR itself is
    the real validator (an unknown ticker fails at discovery); this only
    stops obvious typos before a request is sent."""
    ticker = raw.strip().upper()
    return ticker if TICKER_RE.fullmatch(ticker) else None


def citation_numbers(claims: list[dict]) -> dict[int, int]:
    """Number each cited chunk once, in order of first appearance across the
    claims — so the same source keeps the same [n] everywhere it's cited."""
    numbers: dict[int, int] = {}
    for claim in claims:
        for chunk_id in claim.get("chunk_ids", []):
            if chunk_id not in numbers:
                numbers[chunk_id] = len(numbers) + 1
    return numbers


def source_title(citation: dict) -> str:
    form = citation["form_type"]
    return (
        f"{citation['ticker']} {form} filed {format_date(citation['filed_at'])}, "
        f"{section_label(citation['section'])}"
    )


def excerpt_html(text: str, *, max_chars: int = 900) -> str:
    """Escape a filing excerpt for safe HTML display, trimming at a word
    boundary. Filing text is untrusted input — it is never rendered as
    markup."""
    text = " ".join(text.split())
    if len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars)
        text = text[: cut if cut > 0 else max_chars] + " …"
    return html.escape(text)


def coverage_fraction(embedded: int, total: int) -> float:
    return embedded / total if total else 0.0


EVENT_LABELS: dict[str, str] = {
    "filing.discovered": "Found on EDGAR",
    "filing.fetched": "Downloaded",
    "filing.parsed": "Split into sections",
    "filing.embedded": "Made searchable",
    "filing.parse_failed": "Couldn't parse",
}


def event_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type)


def event_detail(event: dict) -> str:
    payload = event.get("payload") or {}
    if "chunk_count" in payload:
        n = payload["chunk_count"]
        return f"{n} passage{'s' if n != 1 else ''} indexed"
    if "section_count" in payload:
        n = payload["section_count"]
        return f"{n} section{'s' if n != 1 else ''}"
    if "reason" in payload:
        return str(payload["reason"])
    if "ticker" in payload:
        form = payload.get("form_type")
        return f"{payload['ticker']} {form}" if form else payload["ticker"]
    return ""
