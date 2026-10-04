"""Ask: cited Q&A over every filing FilingSage has read (POST /qa)."""

from __future__ import annotations

import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError
from filingsage_ui.components import (
    ask_and_store,
    cached_companies,
    masthead,
    render_conversation,
    show_api_error,
)

EXAMPLES = [
    "What are Apple's main competitive risks?",
    "How did Tesla describe demand in its latest quarter?",
    "Are there any ongoing legal proceedings at JPMorgan?",
]
FORMS = ["All forms", "10-K", "10-Q", "8-K"]
STATE = "conversation"

st.session_state.setdefault(STATE, [])


def _queue_example() -> None:
    # The pills' on_change callback is the one place a widget's own state
    # may be reset, so a failed example can't re-fire on every rerun.
    st.session_state.pending_question = st.session_state.example
    st.session_state.example = None


masthead(
    "Ask the filings",
    "Answers come only from SEC filings FilingSage has read. Every claim points to the "
    "passage it came from, and if the filings don't cover it, you'll be told so. Name a "
    "company in your question and the search is limited to it.",
    eyebrow="Research assistant",
)

try:
    companies = cached_companies()
except ApiError as err:
    show_api_error(err)
    st.stop()

searchable = [c for c in companies if c["embedded"] > 0]
main, side = st.columns([2.4, 1], gap="large")

with side:
    st.subheader("Searchable now", anchor=False)
    if searchable:
        for c in searchable:
            st.markdown(
                f"**{c['ticker']}** {c['name']}  \n"
                f":gray[{c['embedded']} of {c['filings']} filings, latest "
                f"{fmt.format_date(c['latest_filed_at'])}]"
            )
    else:
        st.caption("Nothing yet.")

with main:
    if not searchable:
        st.info(
            "No filings are searchable yet. Add a company on the **Filings** page; its "
            "filings become searchable here as they finish processing.",
            icon=":material/inbox:",
        )
        st.page_link("views/filings.py", label="Add a company", icon=":material/add:")
        st.stop()

    labels = {"Any company (detect from question)": None} | {
        f"{c['ticker']} ({c['name']})": c["ticker"] for c in searchable
    }
    c1, c2, c3 = st.columns([1.7, 1.4, 1])
    company = c1.selectbox("Company", list(labels))
    form = c2.segmented_control("Form", FORMS, default="All forms")
    since = c3.date_input("Filed on or after", value=None, format="YYYY-MM-DD")
    scope = {
        "ticker": labels[company],
        "form_type": None if form in (None, "All forms") else form,
        "since": since.isoformat() if since else None,
    }

    if not st.session_state[STATE]:
        st.pills(
            "Try asking", EXAMPLES, selection_mode="single", key="example",
            on_change=_queue_example,
        )

    render_conversation(STATE)

    if st.session_state[STATE] and st.button("Clear conversation", type="tertiary"):
        st.session_state[STATE] = []
        st.session_state[f"{STATE}_error"] = None
        st.rerun()

typed = st.chat_input("Ask about risks, results, guidance, legal matters…", max_chars=500)
question = (typed or st.session_state.pop("pending_question", None) or "").strip()
if question:
    with main, st.spinner("Reading the filings…"):
        ask_and_store(question, scope, STATE)
    st.rerun()
