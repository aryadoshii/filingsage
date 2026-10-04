"""Pipeline: live view of ingestion — what's searchable, what's in flight,
and the latest events from the transactional event log."""

from __future__ import annotations

import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError
from filingsage_ui.components import client, masthead, show_api_error

masthead(
    "Pipeline",
    "How FilingSage turns filings into searchable passages: found on EDGAR, downloaded, "
    "split into sections, then indexed. This page refreshes every five seconds.",
    eyebrow="System status",
)


@st.fragment(run_every="5s")
def live() -> None:
    api = client()
    try:
        health = api.health()
        stats = api.stats()
        companies = api.companies()
        events = api.events(limit=20)
    except ApiError as err:
        show_api_error(err)
        return

    by_status = stats["filings_by_status"]
    in_progress = sum(by_status.get(s, 0) for s in fmt.IN_PROGRESS_STATUSES)

    cols = st.columns(5)
    cols[0].metric("Companies", stats["companies"])
    cols[1].metric("Filings found", stats["total_filings"])
    cols[2].metric("Searchable", by_status.get("embedded", 0))
    cols[3].metric("Processing", in_progress)
    cols[4].metric("Passages indexed", f"{stats['embedded_chunks']:,}")

    quarantined = by_status.get("quarantined", 0)
    st.caption(
        f"API {health['status']} (version {health['version']}). Last pipeline activity "
        f"{fmt.relative_time(stats['last_event_at'])}."
        + (f" {quarantined} filing(s) couldn't be parsed and were set aside." if quarantined else "")
    )

    left, right = st.columns([1.15, 1], gap="large")
    with left:
        st.subheader("Coverage by company", anchor=False)
        if companies:
            st.dataframe(
                [
                    {
                        "Company": c["ticker"],
                        "Name": c["name"],
                        "Searchable": fmt.coverage_fraction(c["embedded"], c["filings"]),
                        "Filings": f"{c['embedded']} of {c['filings']}",
                        "Latest filing": c["latest_filed_at"],
                    }
                    for c in companies
                ],
                hide_index=True,
                width="stretch",
                column_config={
                    "Searchable": st.column_config.ProgressColumn(
                        "Searchable", format="percent", min_value=0.0, max_value=1.0,
                        help="Share of this company's filings that can be searched",
                    ),
                    "Latest filing": st.column_config.DateColumn(format="D MMM YYYY"),
                },
            )
        else:
            st.caption("No companies tracked yet.")

    with right:
        st.subheader("Recent activity", anchor=False)
        if events:
            st.dataframe(
                [
                    {
                        "When": fmt.relative_time(e["created_at"]),
                        "Step": fmt.event_label(e["type"]),
                        "Filing": e["entity_id"],
                        "Detail": fmt.event_detail(e),
                    }
                    for e in events
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("No activity yet.")


live()
