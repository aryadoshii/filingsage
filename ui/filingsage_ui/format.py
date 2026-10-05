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
    "filing.requeued": "Retried",
    "filing.recovery_reset": "Restarted from download",
    "company.refreshed": "Financials refreshed",
    "company.refresh_failed": "Financials refresh failed",
    "ingest.completed": "Checked EDGAR",
    "ingest.failed": "Couldn't check EDGAR",
    "pipeline.reconciled": "Checked for stuck filings",
    # filing.failed is labelled from its payload: "Failed at <step>".
}

# The pipeline step a failure event names -> the word a reader uses for it.
STEP_LABELS: dict[str, str] = {
    "fetch": "download",
    "parse": "parsing",
    "embed": "indexing",
    "refresh": "financials refresh",
    "ingest": "EDGAR check",
}

# Why the reconciler restarted a filing, in place of storage-layer jargon.
RESET_REASONS: dict[str, str] = {
    "bronze missing on disk": "Downloaded file was missing, so it's being downloaded again",
    "silver missing on disk": "Parsed file was missing, so it's being processed again",
}

# Event entity ids that aren't a filing or a ticker.
EVENT_SUBJECTS: dict[str, str] = {
    "watchlist": "All tracked companies",
    "pipeline": "All filings",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def event_label(event_type: str, payload: dict | None = None) -> str:
    """What happened, in plain words. Never the raw event type: an event
    this table doesn't know yet still reads as words ("analysis.completed"
    -> "Analysis completed")."""
    if event_type == "filing.failed":
        step = (payload or {}).get("step", "")
        return f"Failed at {STEP_LABELS.get(step, step.replace('_', ' ') or 'an unknown step')}"
    if event_type in EVENT_LABELS:
        return EVENT_LABELS[event_type]
    return event_type.replace(".", " ").replace("_", " ").capitalize()


def event_subject(event: dict) -> str:
    entity = event.get("entity_id", "")
    return EVENT_SUBJECTS.get(entity, entity)


def event_detail(event: dict) -> str:
    payload = event.get("payload") or {}
    kind = event.get("type", "")
    if kind in ("filing.failed", "company.refresh_failed", "ingest.failed"):
        attempts = payload.get("attempts")
        tries = f" (after {_plural(attempts, 'attempt')})" if attempts and attempts > 1 else ""
        return f"{payload.get('error', 'Unknown error')}{tries}"
    if kind == "company.refreshed":
        return _plural(payload.get("facts", 0), "financial fact")
    if kind == "ingest.completed":
        return _plural(payload.get("inserted", 0), "new filing")
    if kind == "filing.requeued":
        return f"Was stuck at: {status_label(payload.get('from_status', ''))[0]}"
    if kind == "filing.recovery_reset":
        reason = payload.get("reason", "")
        return RESET_REASONS.get(reason, reason)
    if kind == "pipeline.reconciled":
        retried = payload.get("requeued", 0)
        text = f"{_plural(retried, 'stuck filing')} retried" if retried else "Nothing stuck"
        attention = payload.get("needs_attention", 0)
        return text + (f"; {attention} need attention" if attention else "")
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


# /readyz keys -> display names, in the order the dashboard shows them.
DEPENDENCY_LABELS: dict[str, str] = {"postgres": "Postgres", "redis": "Redis", "qdrant": "Qdrant"}


def dependency_status(readiness: dict) -> list[tuple[str, bool]]:
    """(name, is_ok) per dependency, from /readyz's body. A dependency the
    API didn't report counts as down rather than silently missing."""
    return [(label, readiness.get(key) == "ok") for key, label in DEPENDENCY_LABELS.items()]


# --- numbers -------------------------------------------------------------------

MINUS = "−"  # a real minus sign, not a hyphen: aligns with digits


def money(value: float | None, *, decimals: int = 1) -> str:
    """$391.0B / $512.3M / $12,345 — the scale a finance reader expects."""
    if value is None:
        return "—"
    sign = MINUS if value < 0 else ""
    v = abs(value)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= threshold:
            return f"{sign}${v / threshold:,.{decimals}f}{suffix}"
    return f"{sign}${v:,.0f}"


def pct(value: float | None, *, signed: bool = False, decimals: int = 1) -> str:
    if value is None:
        return "—"
    text = f"{abs(value) * 100:.{decimals}f}%"
    if value < 0:
        return MINUS + text
    return ("+" + text) if signed and value > 0 else text


def per_share(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{MINUS if value < 0 else ''}${abs(value):,.2f}"


FORMATTERS = {"money": money, "pct": pct, "eps": per_share}


def period_label(end: str, period: str) -> str:
    """Column header for a statement period: 'Sep 2025' or 'FY Sep 2025'.
    Calendar labels on purpose: fiscal quarter names differ by company
    (Apple's fiscal Q1 ends in December), period-end months don't."""
    d = date.fromisoformat(end)
    label = d.strftime("%b %Y")
    return f"FY {label}" if period == "annual" else label


# Statement layout: (metric, label, format), with section headings as
# (None, heading, None). Order follows a standard 10-K presentation.
STATEMENT_ROWS: list[tuple[str | None, str, str | None]] = [
    (None, "Income statement", None),
    ("revenue", "Revenue", "money"),
    ("revenue_growth", "Revenue growth, year over year", "pct"),
    ("gross_profit", "Gross profit", "money"),
    ("gross_margin", "Gross margin", "pct"),
    ("operating_income", "Operating income", "money"),
    ("operating_margin", "Operating margin", "pct"),
    ("net_income", "Net income", "money"),
    ("net_margin", "Net margin", "pct"),
    ("eps_diluted", "Earnings per share (diluted)", "eps"),
    (None, "Cash flow", None),
    ("operating_cash_flow", "Operating cash flow", "money"),
    ("capex", "Capital expenditures", "money"),
    ("free_cash_flow", "Free cash flow", "money"),
    (None, "Balance sheet", None),
    ("cash", "Cash and equivalents", "money"),
    ("total_assets", "Total assets", "money"),
    ("total_liabilities", "Total liabilities", "money"),
    ("equity", "Shareholders' equity", "money"),
    ("long_term_debt", "Long-term debt", "money"),
]

KEY_STAT_LABELS: dict[str, tuple[str, str]] = {
    "revenue_ttm": ("Revenue, last 12 months", "money"),
    "net_income_ttm": ("Net income, last 12 months", "money"),
    "net_margin_ttm": ("Net margin, last 12 months", "pct"),
    "free_cash_flow_ttm": ("Free cash flow, last 12 months", "money"),
    "eps_diluted_fy": ("EPS (diluted), last fiscal year", "eps"),
    "cash": ("Cash and equivalents", "money"),
    "long_term_debt": ("Long-term debt", "money"),
    "revenue_growth_yoy": ("Revenue growth, latest quarter", "pct"),
}


def statement_csv(columns: list[dict], period: str) -> str:
    """The statement as CSV with raw numbers (not display strings), one row
    per metric — what someone downloading it wants to put in a spreadsheet."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["metric", *[period_label(c["end"], period) for c in columns]])
    for metric, label, kind in STATEMENT_ROWS:
        if metric is None:
            continue
        writer.writerow([label, *[
            "" if c["values"].get(metric) is None else c["values"][metric] for c in columns
        ]])
    return buf.getvalue()


def is_recent(filed_at: str | None, *, days: int = 7, today: date | None = None) -> bool:
    if not filed_at:
        return False
    return ((today or date.today()) - date.fromisoformat(filed_at)).days <= days
