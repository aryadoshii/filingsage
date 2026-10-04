"""Ask: cited Q&A over the filings FilingSage has read (POST /qa)."""

from __future__ import annotations

import html

import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError
from filingsage_ui.components import cached_companies, client, page_header, show_api_error

EXAMPLES = [
    "What are the main competitive risks?",
    "How did revenue change in the latest quarter?",
    "Are there any ongoing legal proceedings?",
]
FORMS = ["All forms", "10-K", "10-Q", "8-K"]

if "conversation" not in st.session_state:
    st.session_state.conversation = []  # [{question, scope, answer, citations}]
st.session_state.setdefault("ask_error", None)


def _queue_example() -> None:
    # Runs as the pills' on_change callback, the one place a widget's own
    # state may be reset — so a failed example can't re-fire on every rerun.
    st.session_state.pending_question = st.session_state.example
    st.session_state.example = None


def render_answer(turn: dict) -> None:
    answer = turn["answer"]
    st.html(f'<div class="fs-answer">{html.escape(answer["answer"])}</div>')

    if answer["insufficient_evidence"] or not answer["claims"]:
        st.badge(
            "Not enough evidence in the filings",
            icon=":material/search_off:",
            color="gray",
            help="FilingSage only answers from filings it has read. Try a different company, "
            "a broader question, or add more filings on the Filings page.",
        )
        return

    st.badge(
        f"Model confidence: {answer['confidence']}",
        icon=":material/psychology:",
        color="blue",
        help="The language model's own estimate. Independent claim verification is on the "
        "roadmap; until then, check each claim against its highlighted source below.",
    )

    numbers = fmt.citation_numbers(answer["claims"])
    items = []
    for claim in answer["claims"]:
        refs = "".join(f'<span class="fs-ref">{numbers[cid]}</span>' for cid in claim["chunk_ids"])
        # Glue the markers to the claim's last word so they never wrap onto
        # a line of their own.
        *head, last = html.escape(claim["text"]).rsplit(" ", 1) or [""]
        tail = f'<span class="fs-refs">{last}{refs}</span>'
        items.append(f"<li>{' '.join([*head, tail])}</li>")
    st.markdown("**What this is based on**")
    st.html(f'<ul class="fs-claims">{"".join(items)}</ul>')

    by_id = {c["chunk_id"]: c for c in turn["citations"]}
    st.markdown("**Sources**")
    for position, (chunk_id, n) in enumerate(numbers.items()):
        citation = by_id.get(chunk_id)
        if citation is None:
            st.caption(f"[{n}] Source passage {chunk_id} is no longer in the database.")
            continue
        with st.expander(f"{n}. {fmt.source_title(citation)}", expanded=position == 0):
            st.html(
                f'<div class="fs-excerpt"><mark>{fmt.excerpt_html(citation["text"])}</mark></div>'
                f'<div class="fs-meta">{html.escape(citation["company"])}, accession '
                f'{html.escape(citation["accession_no"])}</div>'
            )
            st.link_button(
                "Open filing on sec.gov", citation["edgar_url"], icon=":material/open_in_new:"
            )


def ask(question: str, scope: dict) -> None:
    try:
        answer = client().ask(question, **scope)
        cited = sorted({cid for claim in answer["claims"] for cid in claim["chunk_ids"]})
        citations = client().citations(cited)
    except ApiError as err:
        st.session_state.ask_error = str(err)
        return
    st.session_state.ask_error = None
    st.session_state.conversation.append(
        {"question": question, "scope": scope, "answer": answer, "citations": citations}
    )


page_header(
    "Ask the filings",
    "Answers come only from SEC filings FilingSage has read. Every claim points to the "
    "passage it came from, and if the filings don't cover it, you'll be told so.",
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

    labels = {"All companies": None} | {
        f"{c['ticker']} ({c['name']})": c["ticker"] for c in searchable
    }
    c1, c2, c3 = st.columns([1.6, 1.4, 1])
    company = c1.selectbox("Company", list(labels))
    form = c2.segmented_control("Form", FORMS, default="All forms")
    since = c3.date_input("Filed on or after", value=None, format="YYYY-MM-DD")
    scope = {
        "ticker": labels[company],
        "form_type": None if form in (None, "All forms") else form,
        "since": since.isoformat() if since else None,
    }

    if not st.session_state.conversation:
        st.pills(
            "Try asking", EXAMPLES, selection_mode="single", key="example",
            on_change=_queue_example,
        )

    for turn in st.session_state.conversation:
        with st.chat_message("user", avatar=":material/person:"):
            where = turn["scope"]["ticker"] or "all companies"
            st.markdown(f"{turn['question']}  \n:gray[Searched {where}]")
        with st.chat_message("assistant", avatar=":material/plagiarism:"):
            render_answer(turn)

    if st.session_state.ask_error:
        st.error(st.session_state.ask_error, icon=":material/error:")

    if st.session_state.conversation and st.button("Clear conversation", type="tertiary"):
        st.session_state.conversation = []
        st.session_state.ask_error = None
        st.rerun()

typed = st.chat_input("Ask about risks, results, guidance, legal matters…", max_chars=500)
question = (typed or st.session_state.pop("pending_question", None) or "").strip()
if question:
    with main, st.spinner("Reading the filings…"):
        ask(question, scope)
    st.rerun()
