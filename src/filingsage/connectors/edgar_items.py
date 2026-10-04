"""8-K item codes -> what happened, in plain language.

Every 8-K declares which SEC-defined items it reports (Form 8-K General
Instructions, Items 1.01-9.01); EDGAR lists them per filing ("2.02,9.01").
That's enough to turn a feed of 8-Ks into an events timeline — "Released
earnings results", "Executive or board change" — without reading the
document. The headline for a specific filing comes later, from its brief.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ItemInfo:
    label: str
    notable: bool = False  # worth flagging to a reader: often signals trouble or big change


ITEMS: dict[str, ItemInfo] = {
    "1.01": ItemInfo("Signed a material agreement"),
    "1.02": ItemInfo("Ended a material agreement"),
    "1.03": ItemInfo("Bankruptcy or receivership", notable=True),
    "1.04": ItemInfo("Mine safety violation"),
    "1.05": ItemInfo("Reported a cybersecurity incident", notable=True),
    "2.01": ItemInfo("Completed an acquisition or sale of assets"),
    "2.02": ItemInfo("Released earnings results"),
    "2.03": ItemInfo("Took on a new debt obligation"),
    "2.04": ItemInfo("A debt obligation was accelerated", notable=True),
    "2.05": ItemInfo("Announced restructuring or exit costs", notable=True),
    "2.06": ItemInfo("Recorded a material impairment", notable=True),
    "3.01": ItemInfo("Delisting notice or listing transfer", notable=True),
    "3.02": ItemInfo("Sold unregistered shares"),
    "3.03": ItemInfo("Changed shareholder rights"),
    "4.01": ItemInfo("Changed auditor", notable=True),
    "4.02": ItemInfo("Past financial statements can no longer be relied on", notable=True),
    "5.01": ItemInfo("Change in control of the company", notable=True),
    "5.02": ItemInfo("Executive or board change"),
    "5.03": ItemInfo("Amended charter or bylaws"),
    "5.04": ItemInfo("Paused trading in employee benefit plans"),
    "5.05": ItemInfo("Changed code of ethics"),
    "5.06": ItemInfo("Changed shell company status"),
    "5.07": ItemInfo("Shareholder vote results"),
    "5.08": ItemInfo("Shareholder director nominations"),
    "7.01": ItemInfo("Investor communication (Reg FD)"),
    "8.01": ItemInfo("Other material event"),
    "9.01": ItemInfo("Financial statements and exhibits"),
}

# Attached to most 8-Ks alongside the real item; only shown when alone.
EXHIBITS_ONLY = "9.01"


def parse_items(raw: str | None) -> list[str]:
    return [code.strip() for code in (raw or "").split(",") if code.strip()]


def describe(codes: list[str]) -> list[ItemInfo]:
    """Plain-language events for a filing's item codes, exhibits dropped
    unless they're the only item. Unknown codes are kept, by number."""
    shown = [c for c in codes if c != EXHIBITS_ONLY] or codes
    return [ITEMS.get(code, ItemInfo(f"Item {code}")) for code in shown]
