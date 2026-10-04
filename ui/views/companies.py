"""Companies: the home page — every tracked company as a card with its
headline numbers, straight from its SEC filings."""

from __future__ import annotations

import html

import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError
from filingsage_ui.components import (
    cached_overview,
    masthead,
    show_api_error,
    signed_change_html,
)

masthead(
    "Research companies from what they file",
    "Financials, events and answers drawn only from SEC filings, with every number and "
    "claim traceable to its source. Pick a company to open its research page.",
    eyebrow="FilingSage",
)

try:
    rows = cached_overview()
except ApiError as err:
    show_api_error(err)
    st.stop()

if not rows:
    st.info(
        "No companies tracked yet. Add one on the **Filings** page to start.",
        icon=":material/inbox:",
    )
    st.page_link("views/filings.py", label="Track a company", icon=":material/add:")
    st.stop()

c1, c2 = st.columns([2, 1])
query = c1.text_input("Find a company", placeholder="Ticker or name", label_visibility="collapsed")
order = c2.segmented_control(
    "Sort by", ["Revenue", "Growth", "Latest filing"], default="Revenue",
    label_visibility="collapsed",
)

if query:
    q = query.strip().lower()
    rows = [r for r in rows if q in r["ticker"].lower() or q in r["name"].lower()]
sort_keys = {
    "Revenue": lambda r: r["revenue_ttm"] or 0,
    "Growth": lambda r: r["revenue_growth_yoy"] if r["revenue_growth_yoy"] is not None else -9,
    "Latest filing": lambda r: r["latest_filed_at"] or "",
}
rows = sorted(rows, key=sort_keys.get(order or "Revenue"), reverse=True)

if not rows:
    st.caption("No tracked company matches that search.")

PER_ROW = 3
for start in range(0, len(rows), PER_ROW):
    cols = st.columns(PER_ROW, gap="medium")
    for col, r in zip(cols, rows[start : start + PER_ROW], strict=False):
        with col.container(border=True):
            new = '<span class="fs-new">New filing</span>' if fmt.is_recent(r["latest_filed_at"]) else ""
            st.html(
                f'<div class="fs-card-head"><span class="t">{html.escape(r["ticker"])}</span>'
                f'<span class="n">{html.escape(r["name"])}</span></div>'
                f'<div class="fs-card-sector">{html.escape(r["sector"] or "Sector not yet loaded")}</div>'
                f'<div class="fs-figure">{fmt.money(r["revenue_ttm"])}</div>'
                f'<div class="fs-figure-label">Revenue, last 12 months</div>'
                f'<div style="margin:0.55rem 0 0.2rem 0">'
                f'{signed_change_html(r["revenue_growth_yoy"], "vs a year ago")}</div>'
                f'<div class="fs-meta">Net margin {fmt.pct(r["net_margin_ttm"])}, '
                f'latest filing {fmt.format_date(r["latest_filed_at"])}{new}</div>'
            )
            st.page_link(
                "views/company.py", label="Open research page",
                icon=":material/arrow_forward:", query_params={"ticker": r["ticker"]},
            )

st.caption(
    "Figures come from the companies' XBRL financial data filed with the SEC. Revenue "
    "growth compares the latest quarter with the same quarter a year earlier."
)
