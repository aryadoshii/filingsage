"""Streamlit rendering helpers shared by the views. Everything that touches
`st` lives here or in views/; format.py and api.py stay framework-free."""

from __future__ import annotations

import streamlit as st

from filingsage_ui import format as fmt
from filingsage_ui.api import ApiError, FilingSageClient

STYLESHEET = """
<style>
/* Reading text — answers and filing excerpts — is set in a serif, like the
   documents it comes from. Interface chrome stays in the sans. */
.fs-answer {
  font-family: "Source Serif 4", Georgia, serif;
  font-size: 1.075rem;
  line-height: 1.65;
  max-width: 68ch;
  color: #1B2230;
  margin: 0 0 0.75rem 0;
}
.fs-lede { color: #4A5465; max-width: 68ch; margin: -0.25rem 0 1.25rem 0; }

/* Claims: plain list, each ending in its source markers. */
.fs-claims { margin: 0.25rem 0 0.5rem 0; padding-left: 1.1rem; max-width: 72ch; }
.fs-claims li { margin: 0.3rem 0; line-height: 1.5; }
.fs-refs { white-space: nowrap; }
.fs-ref {
  display: inline;
  font-size: 0.72rem;
  font-weight: 600;
  color: #1F4E79;
  background: #E8EFF7;
  border-radius: 4px;
  padding: 0 0.32rem;
  margin-left: 0.2rem;
  vertical-align: 0.15em;
}

/* The one loud element: cited filing text, highlighted the way an analyst
   marks up a printed 10-K. Highlighter yellow is used nowhere else. */
.fs-excerpt {
  font-family: "Source Serif 4", Georgia, serif;
  font-size: 0.98rem;
  line-height: 1.7;
  max-width: 72ch;
  margin: 0.25rem 0 0.5rem 0;
}
.fs-excerpt mark {
  background: linear-gradient(180deg, transparent 12%, #FFEB8A 12%, #FFEB8A 88%, transparent 88%);
  color: inherit;
  padding: 0 0.1rem;
  -webkit-box-decoration-break: clone;
  box-decoration-break: clone;
}
.fs-meta { color: #5B6474; font-size: 0.85rem; }

/* Status: a colored dot always paired with its label, never color alone. */
.fs-status { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.9rem; }
.fs-status::before {
  content: ""; width: 0.55rem; height: 0.55rem; border-radius: 50%;
  background: var(--fs-tone, #8A94A6);
}
.fs-status.good { --fs-tone: #2F7D57; }
.fs-status.pending { --fs-tone: #A86B12; }
.fs-status.bad { --fs-tone: #B23B3B; }

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


def page_header(title: str, lede: str) -> None:
    st.title(title, anchor=False)
    st.html(f'<p class="fs-lede">{lede}</p>')


def status_html(status: str) -> str:
    label, tone = fmt.status_label(status)
    return f'<span class="fs-status {tone}">{label}</span>'


def show_api_error(err: ApiError) -> None:
    st.error(str(err), icon=":material/error:")


@st.cache_data(ttl=15, show_spinner=False)
def cached_companies() -> list[dict]:
    return client().companies()
