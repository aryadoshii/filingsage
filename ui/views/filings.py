"""Filings: add a company to track, and browse what's been ingested."""

from __future__ import annotations

import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError
from filingsage_ui.components import cached_companies, client, masthead, show_api_error

FORMS = ["All forms", "10-K", "10-Q", "8-K"]
STATUS_FILTERS = {"Any status": None} | {
    label: status for status, (label, _tone) in fmt.STATUS_LABELS.items()
}

masthead(
    "Filings",
    "Every 10-K, 10-Q and 8-K FilingSage has found for the companies it tracks. New filings "
    "are picked up automatically every two hours.",
    eyebrow="Coverage",
)

with st.expander("Track a company", icon=":material/add:", expanded=False):
    with st.form("track", clear_on_submit=True, border=False):
        c1, c2 = st.columns([1, 2])
        raw_ticker = c1.text_input("Ticker", placeholder="e.g. NVDA", max_chars=10)
        limit = c2.slider(
            "Most recent filings to read",
            min_value=1, max_value=40, value=10,
            help="New filings are always picked up later; this only sets how far back the "
            "first read goes. More filings take longer to become searchable.",
        )
        submitted = st.form_submit_button("Track company", type="primary")
    if submitted:
        ticker = fmt.normalize_ticker(raw_ticker)
        if ticker is None:
            st.error("Enter a ticker symbol like AAPL or BRK.B.", icon=":material/error:")
        else:
            try:
                client().track([ticker], limit=limit)
            except ApiError as err:
                show_api_error(err)
            else:
                cached_companies.clear()
                st.success(
                    f"Tracking {ticker}. Its filings will appear below as they download, and "
                    "become searchable on the Ask page once processed. Follow progress on the "
                    "Pipeline page.",
                    icon=":material/check_circle:",
                )

try:
    companies = cached_companies()
except ApiError as err:
    show_api_error(err)
    st.stop()

if not companies:
    st.info(
        "No companies tracked yet. Use **Track a company** above to add your first one.",
        icon=":material/inbox:",
    )
    st.stop()

c1, c2, c3 = st.columns([1.4, 1.6, 1])
tickers = {"All companies": None} | {f"{c['ticker']} ({c['name']})": c["ticker"] for c in companies}
company = c1.selectbox("Company", list(tickers))
form = c2.segmented_control("Form", FORMS, default="All forms")
status_label = c3.selectbox("Status", list(STATUS_FILTERS))


@st.fragment(run_every="10s")
def filings_table(ticker: str | None, form_type: str | None, status: str | None) -> None:
    """Refreshes itself every 10s, so filings move from Queued to Searchable
    on screen without a manual reload."""
    try:
        rows = client().filings(ticker=ticker, form_type=form_type, status=status, limit=200)
    except ApiError as err:
        show_api_error(err)
        return
    if not rows:
        st.caption("No filings match these filters.")
        return

    table = [
        {
            "Filed": r["filed_at"],
            "Company": r["ticker"],
            "Form": f"{r['form_type']} {fmt.FORM_DESCRIPTIONS.get(r['form_type'], '')}".strip(),
            "Status": fmt.status_label(r["status"])[0],
            "Passages": r["chunk_count"],
            "Filing": r["edgar_url"],
        }
        for r in rows
    ]
    in_progress = sum(r["status"] in fmt.IN_PROGRESS_STATUSES for r in rows)
    note = f", {in_progress} still processing" if in_progress else ""
    st.caption(f"{len(rows)} filings{note}. Newest first.")
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "Filed": st.column_config.DateColumn("Filed", format="D MMM YYYY"),
            "Passages": st.column_config.NumberColumn(
                "Passages", help="Searchable text passages extracted from the filing"
            ),
            "Filing": st.column_config.LinkColumn("Filing", display_text="Open on sec.gov"),
        },
    )


filings_table(
    tickers[company],
    None if form in (None, "All forms") else form,
    STATUS_FILTERS[status_label],
)
