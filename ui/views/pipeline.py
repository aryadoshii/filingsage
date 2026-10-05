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
        readiness = api.readiness()
        stats = api.stats()
        companies = api.companies()
        events = api.events(limit=20)
        attention = api.needs_attention() if stats.get("needs_attention") else []
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

    quarantined = stats.get("quarantined", by_status.get("quarantined", 0))
    st.caption(
        f"API {health['status']} (version {health['version']}). Last pipeline activity "
        f"{fmt.relative_time(stats['last_event_at'])}."
        + (f" {quarantined} filing(s) couldn't be parsed and were set aside." if quarantined else "")
    )
    st.markdown(
        f"Last checked EDGAR: **{fmt.relative_time(stats.get('last_ingest_at'))}**. "
        f"Last stuck-filing check: **{fmt.relative_time(stats.get('last_reconcile_at'))}**."
    )
    # One line of badges, each with its word as well as its color.
    st.markdown(" ".join(
        f":green-badge[:material/check_circle: {name}: ok]" if ok
        else f":red-badge[:material/error: {name}: down]"
        for name, ok in fmt.dependency_status(readiness)
    ))

    if attention:
        st.subheader("Needs attention", anchor=False)
        st.caption(
            "These filings failed three times and are no longer retried automatically. "
            "The error below is from the most recent attempt."
        )
        st.dataframe(
            [
                {
                    "Company": row["ticker"],
                    "Form": row["form_type"],
                    "Filed": fmt.format_date(row["filed_at"]),
                    "Last error": row["last_error"] or "—",
                    "Attempts": row["attempts"],
                }
                for row in attention
            ],
            hide_index=True,
            width="stretch",
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
                        "Step": fmt.event_label(e["type"], e.get("payload")),
                        "For": fmt.event_subject(e),
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
