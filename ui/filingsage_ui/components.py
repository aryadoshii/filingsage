"""Streamlit rendering helpers shared by the views. Everything that touches
`st` lives here or in views/; format.py and api.py stay framework-free.

Visual system — "an annotated annual report":
  ink #102231 (text, masthead), paper #FFFFFF, rule #DDE3EA,
  teal #0F6E8C (actions, links, chart series 1),
  up #1E7A4C / down #B23B2E (always paired with a +/− sign, never color alone),
  highlighter #FFE98A — reserved for cited filing text and nothing else.
  Source Serif 4 for headings and reading text; IBM Plex Sans with tabular
  figures for interface and numbers, so columns of figures align.
"""

from __future__ import annotations

import html

import altair as alt
import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError, FilingSageClient

INK = "#102231"
MUTED = "#5A6878"
RULE = "#DDE3EA"
TEAL = "#1478A0"
UP = "#1E7A4C"
DOWN = "#B23B2E"
# Margin lines: validated together for colour-vision deficiency
# (dataviz validator, all-pairs, light surface) — teal, orange, green.
SERIES = ["#1478A0", "#E0702F", "#2A9D6F"]

STYLESHEET = """
<style>
:root { --fs-ink:#102231; --fs-muted:#5A6878; --fs-rule:#DDE3EA; --fs-teal:#0F6E8C; }
html, body, [class*="st-"] { font-variant-numeric: tabular-nums; }
[data-testid="stMainBlockContainer"] { padding-top: 4.6rem; }

/* Masthead: the dark band every page opens with. On company pages it's the
   quote header — ticker, name, sector — like a terminal's security banner. */
.fs-mast {
  background: var(--fs-ink); color: #F4F6F8; border-radius: 10px;
  padding: 1.6rem 1.9rem 1.5rem; margin: 0.25rem 0 1.4rem 0;
}
.fs-mast h1 {
  font-family: "Source Serif 4", Georgia, serif; font-weight: 600;
  font-size: 2.15rem; line-height: 1.15; margin: 0; color: #FFFFFF; padding: 0;
}
.fs-mast p { color: #B9C4CF; margin: 0.55rem 0 0 0; max-width: 70ch; font-size: 1.02rem; }
.fs-mast .fs-ticker {
  font-family: "IBM Plex Sans", sans-serif; font-weight: 600; font-size: 0.95rem;
  letter-spacing: 0.04em; color: #8FD3EA; margin-bottom: 0.35rem;
}
.fs-mast .fs-facts { display: flex; flex-wrap: wrap; gap: 0.4rem 1.6rem; margin-top: 0.9rem; }
.fs-mast .fs-facts span { color: #D5DDE5; font-size: 0.92rem; }
.fs-mast .fs-facts b { color: #8C9AA8; font-weight: 500; margin-right: 0.35rem; }

/* Reading text — answers and filing excerpts — in the serif. */
.fs-answer {
  font-family: "Source Serif 4", Georgia, serif; font-size: 1.075rem;
  line-height: 1.65; max-width: 68ch; color: var(--fs-ink); margin: 0 0 0.75rem 0;
}
.fs-claims { margin: 0.25rem 0 0.5rem 0; padding-left: 1.1rem; max-width: 72ch; }
.fs-claims li { margin: 0.3rem 0; line-height: 1.5; }
.fs-refs { white-space: nowrap; }
.fs-ref {
  display: inline; font-size: 0.72rem; font-weight: 600; color: var(--fs-teal);
  background: #E5F1F6; border-radius: 4px; padding: 0 0.32rem; margin-left: 0.2rem;
  vertical-align: 0.15em;
}
/* The one loud element: cited filing text, highlighted the way an analyst
   marks up a printed 10-K. */
.fs-excerpt {
  font-family: "Source Serif 4", Georgia, serif; font-size: 0.98rem;
  line-height: 1.7; max-width: 72ch; margin: 0.25rem 0 0.5rem 0;
}
.fs-excerpt mark {
  background: linear-gradient(180deg, transparent 12%, #FFE98A 12%, #FFE98A 88%, transparent 88%);
  color: inherit; padding: 0 0.1rem;
  -webkit-box-decoration-break: clone; box-decoration-break: clone;
}
.fs-meta { color: var(--fs-muted); font-size: 0.85rem; }

/* Company cards on the overview grid. */
.fs-card-head { display: flex; align-items: baseline; gap: 0.6rem; }
.fs-card-head .t { font-weight: 600; font-size: 1.15rem; color: var(--fs-ink); }
.fs-card-head .n { color: var(--fs-muted); font-size: 0.92rem; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.fs-card-sector { color: var(--fs-muted); font-size: 0.82rem; margin: 0.1rem 0 0.7rem 0; }
.fs-figure { font-size: 1.55rem; font-weight: 600; color: var(--fs-ink); line-height: 1.1; }
.fs-figure-label { color: var(--fs-muted); font-size: 0.8rem; margin-top: 0.15rem; }
.fs-up { color: #1E7A4C; font-weight: 600; }
.fs-down { color: #B23B2E; font-weight: 600; }
.fs-new {
  display: inline-block; font-size: 0.72rem; font-weight: 600; color: #0F5470;
  background: #DDF0F7; border-radius: 999px; padding: 0.05rem 0.5rem; margin-left: 0.4rem;
}

/* Financial statement table: right-aligned tabular figures, section rules. */
.fs-table-wrap { overflow-x: auto; border: 1px solid var(--fs-rule); border-radius: 8px; }
table.fs-statement { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
.fs-statement th, .fs-statement td { padding: 0.42rem 0.8rem; white-space: nowrap; }
.fs-statement thead th {
  text-align: right; color: var(--fs-muted); font-weight: 500;
  border-bottom: 1px solid var(--fs-rule); background: #F7F9FA;
}
.fs-statement thead th:first-child, .fs-statement td:first-child { text-align: left; }
.fs-statement td { text-align: right; color: var(--fs-ink); border-bottom: 1px solid #EEF1F4; }
.fs-statement tr.section td {
  font-family: "Source Serif 4", Georgia, serif; font-weight: 600; font-size: 0.98rem;
  background: #FBFCFD; padding-top: 0.75rem; border-bottom: 1px solid var(--fs-rule);
}
.fs-statement tr.ratio td { color: var(--fs-muted); font-size: 0.86rem; }
.fs-statement sup { color: var(--fs-teal); font-weight: 600; }

/* Events timeline. */
.fs-event { display: grid; grid-template-columns: 7.5rem 1fr; gap: 0.9rem;
  padding: 0.7rem 0; border-bottom: 1px solid #EEF1F4; }
.fs-event .d { color: var(--fs-muted); font-size: 0.9rem; }
.fs-event .e { color: var(--fs-ink); }
.fs-event a { color: var(--fs-teal); text-decoration: none; font-weight: 500; }
.fs-event a:hover { text-decoration: underline; }
.fs-flag { display: inline-block; font-size: 0.72rem; font-weight: 600; color: #8A2A21;
  background: #F8E3E0; border-radius: 4px; padding: 0.05rem 0.45rem; margin-left: 0.45rem; }

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
</style>
"""


def apply_styles() -> None:
    st.html(STYLESHEET)


@st.cache_resource
def client() -> FilingSageClient:
    return FilingSageClient()


def masthead(title: str, lede: str = "", *, eyebrow: str = "", facts: list[tuple[str, str]] = ()) -> None:
    """The dark band each page opens with. `facts` render as label/value
    pairs under the title (sector, exchange, fiscal year end...)."""
    parts = ['<div class="fs-mast">']
    if eyebrow:
        parts.append(f'<div class="fs-ticker">{html.escape(eyebrow)}</div>')
    parts.append(f"<h1>{html.escape(title)}</h1>")
    if lede:
        parts.append(f"<p>{html.escape(lede)}</p>")
    if facts:
        parts.append('<div class="fs-facts">' + "".join(
            f"<span><b>{html.escape(k)}</b>{html.escape(v)}</span>" for k, v in facts
        ) + "</div>")
    parts.append("</div>")
    st.html("".join(parts))


def show_api_error(err: ApiError) -> None:
    st.error(str(err), icon=":material/error:")


@st.cache_data(ttl=15, show_spinner=False)
def cached_companies() -> list[dict]:
    return client().companies()


@st.cache_data(ttl=30, show_spinner=False)
def cached_overview() -> list[dict]:
    return client().overview()


def signed_change_html(value: float | None, suffix: str = "") -> str:
    """'+12.3% y/y' in green or '−4.1% y/y' in red: the sign carries the
    meaning, the colour reinforces it."""
    if value is None:
        return '<span class="fs-meta">No year-ago quarter yet</span>'
    cls = "fs-up" if value >= 0 else "fs-down"
    return f'<span class="{cls}">{fmt.pct(value, signed=True)}</span> {html.escape(suffix)}'


# --- answers (shared by the Ask page and the company page's Ask tab) ----------


def render_answer(answer: dict, citations: list[dict]) -> None:
    st.html(f'<div class="fs-answer">{html.escape(answer["answer"])}</div>')

    claims = answer.get("claims") or []
    if not claims:
        st.badge(
            "Not enough evidence in the filings",
            icon=":material/search_off:", color="gray",
            help="FilingSage only answers from filings it has read. Try a different company, "
            "a broader question, or add more filings on the Filings page.",
        )
        return

    if answer.get("insufficient_evidence"):
        st.badge(
            "Partial answer: the filings cover only part of this",
            icon=":material/rule:", color="orange",
            help="Some of the question couldn't be answered from the filings; the claims below "
            "are the parts that could.",
        )
    else:
        st.badge(
            f"Model confidence: {answer['confidence']}",
            icon=":material/psychology:", color="blue",
            help="The language model's own estimate. Independent claim verification is on the "
            "roadmap; until then, check each claim against its highlighted source below.",
        )

    numbers = fmt.citation_numbers(claims)
    items = []
    for claim in claims:
        refs = "".join(f'<span class="fs-ref">{numbers[cid]}</span>' for cid in claim["chunk_ids"])
        # Glue the markers to the claim's last word so they never wrap alone.
        *head, last = html.escape(claim["text"]).rsplit(" ", 1) or [""]
        items.append(f"<li>{' '.join([*head, f'<span class=fs-refs>{last}{refs}</span>'])}</li>")
    st.markdown("**What this is based on**")
    st.html(f'<ul class="fs-claims">{"".join(items)}</ul>')

    by_id = {c["chunk_id"]: c for c in citations}
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


def ask_and_store(question: str, scope: dict, state_key: str) -> None:
    """Ask the API, resolve citations, append to the conversation stored at
    st.session_state[state_key]; errors go to f"{state_key}_error"."""
    try:
        answer = client().ask(question, **scope)
        cited = sorted({cid for claim in answer["claims"] for cid in claim["chunk_ids"]})
        citations = client().citations(cited)
    except ApiError as err:
        st.session_state[f"{state_key}_error"] = str(err)
        return
    st.session_state[f"{state_key}_error"] = None
    st.session_state[state_key].append(
        {"question": question, "scope": scope, "answer": answer, "citations": citations}
    )


def render_conversation(state_key: str) -> None:
    for turn in st.session_state.get(state_key, []):
        with st.chat_message("user", avatar=":material/person:"):
            answer = turn["answer"]
            scoped = answer.get("scope_ticker")
            if scoped and answer.get("scope_source") == "question":
                where = f"Searched {scoped}, the company named in your question"
            elif scoped:
                where = f"Searched {scoped}"
            else:
                where = "Searched all companies"
            st.markdown(f"{turn['question']}  \n:gray[{where}]")
        with st.chat_message("assistant", avatar=":material/plagiarism:"):
            render_answer(turn["answer"], turn["citations"])
    if st.session_state.get(f"{state_key}_error"):
        st.error(st.session_state[f"{state_key}_error"], icon=":material/error:")


# --- charts ------------------------------------------------------------------


def _chart_rows(columns: list[dict], metric: str, period: str) -> list[dict]:
    rows = []
    for c in columns:
        value = c["values"].get(metric)
        if value is None:
            continue
        derived = metric in c.get("derived", [])
        rows.append({
            "period": fmt.period_label(c["end"], period),
            "end": c["end"],
            "value": value,
            "shown": fmt.money(value),
            "note": "Derived: full year minus the reported quarters" if derived else "Reported",
            "derived": derived,
        })
    return rows


def bar_chart(columns: list[dict], metric: str, period: str, *, height: int = 230):
    """One metric per chart (never a second axis). Negative values sit below
    the baseline in the down colour; derived periods are drawn lighter and
    say so in the tooltip and the caption under the chart."""
    rows = _chart_rows(columns, metric, period)
    if not rows:
        return None
    order = [r["period"] for r in rows]
    base = alt.Chart(alt.Data(values=rows)).encode(
        x=alt.X("period:N", sort=order, title=None,
                axis=alt.Axis(labelAngle=0, labelColor=MUTED, domainColor=RULE, ticks=False)),
        y=alt.Y("value:Q", title=None,
                axis=alt.Axis(format="~s", labelColor=MUTED, gridColor="#EEF1F4",
                              domain=False, ticks=False, labelExpr="'$' + replace(datum.label, 'G', 'B')")),
        tooltip=[alt.Tooltip("period:N", title="Period"), alt.Tooltip("shown:N", title="Value"),
                 alt.Tooltip("note:N", title="Source")],
    )
    bars = base.mark_bar(cornerRadiusEnd=4, size=26).encode(
        color=alt.condition(alt.datum.value < 0, alt.value(DOWN), alt.value(TEAL)),
        opacity=alt.condition(alt.datum.derived, alt.value(0.45), alt.value(1.0)),
    )
    zero = alt.Chart(alt.Data(values=[{"z": 0}])).mark_rule(color="#9AA6B2").encode(y="z:Q")
    return (bars + zero).properties(height=height).configure_view(stroke=None)


def margin_chart(columns: list[dict], period: str, *, height: int = 230):
    """Gross, operating and net margin over time: three lines, a legend, and
    a direct label at each line's last point so identity never rests on
    colour alone."""
    series = [("gross_margin", "Gross"), ("operating_margin", "Operating"), ("net_margin", "Net")]
    rows = []
    for key, name in series:
        for c in columns:
            v = c["values"].get(key)
            if v is not None:
                rows.append({"period": fmt.period_label(c["end"], period), "end": c["end"],
                             "margin": v, "series": name, "shown": fmt.pct(v)})
    if not rows:
        return None
    order = sorted({(r["end"], r["period"]) for r in rows})
    names = [n for _, n in series if any(r["series"] == n for r in rows)]
    color = alt.Color("series:N", scale=alt.Scale(domain=names, range=SERIES[: len(names)]),
                      legend=alt.Legend(title=None, orient="top", labelColor=INK))
    base = alt.Chart(alt.Data(values=rows)).encode(
        x=alt.X("period:N", sort=[p for _, p in order], title=None,
                axis=alt.Axis(labelAngle=0, labelColor=MUTED, domainColor=RULE, ticks=False)),
        y=alt.Y("margin:Q", title=None,
                axis=alt.Axis(format="%", labelColor=MUTED, gridColor="#EEF1F4",
                              domain=False, ticks=False)),
        color=color,
    )
    lines = base.mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60, filled=True)).encode(
        tooltip=[alt.Tooltip("series:N", title="Margin"), alt.Tooltip("period:N", title="Period"),
                 alt.Tooltip("shown:N", title="Value")],
    )
    last_end = max(r["end"] for r in rows)
    labels = base.transform_filter(alt.datum.end == last_end).mark_text(
        align="left", dx=8, fontSize=11, fontWeight=600
    ).encode(text="shown:N")
    return (lines + labels).properties(height=height).configure_view(stroke=None)


def statement_table_html(columns: list[dict], period: str) -> str:
    head = "".join(f"<th>{fmt.period_label(c['end'], period)}</th>" for c in columns)
    body = []
    for metric, label, kind in fmt.STATEMENT_ROWS:
        if metric is None:
            body.append(f'<tr class="section"><td colspan="{len(columns) + 1}">{label}</td></tr>')
            continue
        if all(c["values"].get(metric) is None for c in columns):
            continue  # a line this company doesn't report (e.g. gross profit at a bank)
        cells = []
        for c in columns:
            text = fmt.FORMATTERS[kind](c["values"].get(metric))
            mark = "<sup>†</sup>" if metric in c.get("derived", []) else ""
            cells.append(f"<td>{text}{mark}</td>")
        cls = ' class="ratio"' if kind == "pct" else ""
        body.append(f"<tr{cls}><td>{html.escape(label)}</td>{''.join(cells)}</tr>")
    return (
        '<div class="fs-table-wrap"><table class="fs-statement">'
        f"<thead><tr><th></th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )
