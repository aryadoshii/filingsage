"""Company research page: quote-style header, key numbers, financial charts
and statements, 8-K events, filings, and Q&A scoped to this company."""

from __future__ import annotations

import html

import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError
from filingsage_ui.components import (
    ask_and_store,
    bar_chart,
    cached_companies,
    client,
    margin_chart,
    masthead,
    render_conversation,
    show_api_error,
    statement_table_html,
)

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

try:
    companies = cached_companies()
except ApiError as err:
    show_api_error(err)
    st.stop()
if not companies:
    st.info("No companies tracked yet. Add one on the Filings page.", icon=":material/inbox:")
    st.stop()

tickers = [c["ticker"] for c in companies]
requested = fmt.normalize_ticker(st.query_params.get("ticker", "")) or tickers[0]
if requested not in tickers:
    st.warning(f"{requested} isn't tracked yet. Showing {tickers[0]} instead.")
    requested = tickers[0]

picked = st.selectbox(
    "Company", tickers, index=tickers.index(requested),
    format_func=lambda t: f"{t}  {next(c['name'] for c in companies if c['ticker'] == t)}",
    label_visibility="collapsed",
)
if picked != st.query_params.get("ticker"):
    st.query_params["ticker"] = picked
ticker = picked

try:
    company = client().company(ticker)
except ApiError as err:
    show_api_error(err)
    st.stop()

facts = []
if company["sector"]:
    facts.append(("Industry", company["sector"]))
if company["exchange"]:
    facts.append(("Exchange", company["exchange"]))
if company["fiscal_year_end"]:
    facts.append(("Fiscal year ends", MONTHS[int(company["fiscal_year_end"][:2]) - 1]))
facts.append(("Filings read", f"{company['embedded']} of {company['filings']}"))
facts.append(("Latest filing", fmt.format_date(company["latest_filed_at"])))
masthead(company["name"], eyebrow=company["ticker"], facts=facts)

# --- key numbers -----------------------------------------------------------------
stats = {s["key"]: s for s in company["key_stats"]}
try:
    quarterly = client().financials(ticker, period="quarter", limit=8)["columns"]
except ApiError as err:
    show_api_error(err)
    quarterly = []


if all(s["value"] is None for s in stats.values()):
    st.info(
        "Financials for this company haven't been loaded yet. They're fetched from the SEC "
        "automatically after it's tracked; refresh in a minute.",
        icon=":material/hourglass_top:",
    )
else:
    ttm = "Trailing twelve months: the sum of the latest four reported quarters."
    tiles = [
        ("Revenue (TTM)", fmt.money(stats["revenue_ttm"]["value"]),
         ttm + " The change compares the latest quarter with the same quarter a year earlier."),
        ("Net income (TTM)", fmt.money(stats["net_income_ttm"]["value"]), ttm),
        ("Net margin (TTM)", fmt.pct(stats["net_margin_ttm"]["value"]),
         "Net income as a share of revenue, trailing twelve months."),
        ("Free cash flow (TTM)", fmt.money(stats["free_cash_flow_ttm"]["value"]),
         "Operating cash flow minus capital expenditures, trailing twelve months."),
        ("EPS, diluted (FY)", fmt.per_share(stats["eps_diluted_fy"]["value"]),
         f"Diluted earnings per share for the fiscal year ended "
         f"{fmt.format_date(stats['eps_diluted_fy']['as_of'])}."),
        ("Cash", fmt.money(stats["cash"]["value"]),
         f"Cash and equivalents as of {fmt.format_date(stats['cash']['as_of'])}."),
    ]
    growth = stats["revenue_growth_yoy"]["value"]
    for i, (col, (label, value, help_text)) in enumerate(zip(st.columns(6), tiles, strict=True)):
        col.metric(
            label, value, help=help_text, border=True, height=128,
            # ASCII sign on purpose: Streamlit reads a leading "-" to pick the
            # arrow direction and colour (fmt.pct uses a typographic minus).
            delta=f"{growth * 100:+.1f}% y/y" if i == 0 and growth is not None else None,
        )

overview, financials_tab, events_tab, filings_tab, ask_tab = st.tabs(
    ["Overview", "Financials", "Events", "Filings", "Ask"]
)

DERIVED_NOTE = (
    "Lighter bars are derived, not reported: companies report the fourth quarter only "
    "inside the annual 10-K, so it's the full year minus the first three quarters."
)

with overview:
    if quarterly:
        a, b = st.columns(2, gap="large")
        with a:
            st.subheader("Revenue by quarter", anchor=False)
            chart = bar_chart(quarterly, "revenue", "quarter")
            if chart:
                st.altair_chart(chart, width="stretch")
            else:
                st.caption("Not reported.")
        with b:
            st.subheader("Net income by quarter", anchor=False)
            chart = bar_chart(quarterly, "net_income", "quarter")
            if chart:
                st.altair_chart(chart, width="stretch")
            else:
                st.caption("Not reported.")
        if any(c["derived"] for c in quarterly):
            st.caption(DERIVED_NOTE)
        st.subheader("Margins", anchor=False)
        chart = margin_chart(quarterly, "quarter")
        if chart:
            st.altair_chart(chart, width="stretch")
        else:
            st.caption("This company doesn't report the lines needed for margins.")
    else:
        st.caption("No quarterly financials yet.")

    st.subheader("Recent events", anchor=False)
    try:
        events = client().company_events(ticker, limit=6)
    except ApiError as err:
        show_api_error(err)
        events = []
    for e in events[:6]:
        flag = '<span class="fs-flag">Worth a look</span>' if e["notable"] else ""
        st.html(
            f'<div class="fs-event"><div class="d">{fmt.format_date(e["filed_at"])}</div>'
            f'<div class="e">{html.escape("; ".join(e["events"]))}{flag}</div></div>'
        )
    if not events:
        st.caption("No 8-K filings yet.")

with financials_tab:
    period_choice = st.segmented_control(
        "Period", ["Quarterly", "Annual"], default="Quarterly", key="fin_period"
    )
    period = "annual" if period_choice == "Annual" else "quarter"
    try:
        statement = client().financials(ticker, period=period, limit=8 if period == "quarter" else 5)
    except ApiError as err:
        show_api_error(err)
        statement = {"columns": []}
    columns = statement["columns"]
    if columns:
        st.html(statement_table_html(columns, period))
        if any(c["derived"] for c in columns):
            st.caption("† Derived: full-year figure minus the reported quarters.")
        st.download_button(
            "Download as CSV", fmt.statement_csv(columns, period),
            file_name=f"{ticker}_{period}_financials.csv", mime="text/csv",
            icon=":material/download:",
        )
        st.caption(
            "From the company's XBRL financial data filed with the SEC. Columns are "
            "labelled by the month each period ended."
        )
    else:
        st.caption("No financials loaded for this company yet.")

with events_tab:
    st.caption(
        "Every 8-K (a report of a significant event) the company has filed, described by the "
        "items it declares. Flagged events are ones investors usually look at closely."
    )
    try:
        all_events = client().company_events(ticker, limit=60)
    except ApiError as err:
        show_api_error(err)
        all_events = []
    for e in all_events:
        flag = '<span class="fs-flag">Worth a look</span>' if e["notable"] else ""
        link = f'<a href="{html.escape(e["edgar_url"])}" target="_blank">Open on sec.gov</a>'
        st.html(
            f'<div class="fs-event"><div class="d">{fmt.format_date(e["filed_at"])}</div>'
            f'<div class="e">{html.escape("; ".join(e["events"]))}{flag}'
            f'<div class="fs-meta">{e["form_type"]}, accession {html.escape(e["accession_no"])}. '
            f"{link}</div></div></div>"
        )
    if not all_events:
        st.caption("No 8-K filings yet.")

with filings_tab:
    try:
        rows = client().filings(ticker=ticker, limit=200)
    except ApiError as err:
        show_api_error(err)
        rows = []
    if rows:
        st.dataframe(
            [
                {
                    "Filed": r["filed_at"],
                    "Form": f"{r['form_type']} {fmt.FORM_DESCRIPTIONS.get(r['form_type'], '')}".strip(),
                    "Status": fmt.status_label(r["status"])[0],
                    "Passages": r["chunk_count"],
                    "Filing": r["edgar_url"],
                }
                for r in rows
            ],
            hide_index=True, width="stretch",
            column_config={
                "Filed": st.column_config.DateColumn("Filed", format="D MMM YYYY"),
                "Filing": st.column_config.LinkColumn("Filing", display_text="Open on sec.gov"),
            },
        )
    else:
        st.caption("No filings yet.")

with ask_tab:
    state_key = f"company_conversation_{ticker}"
    st.session_state.setdefault(state_key, [])
    with st.form(f"ask_{ticker}", clear_on_submit=True, border=False):
        question = st.text_input(
            f"Ask about {company['name']}",
            placeholder="e.g. What are the biggest risks management describes?",
            max_chars=500,
        )
        submitted = st.form_submit_button("Ask", type="primary", icon=":material/send:")
    if submitted and question.strip():
        with st.spinner("Reading the filings…"):
            ask_and_store(question.strip(), {"ticker": ticker}, state_key)
    render_conversation(state_key)
