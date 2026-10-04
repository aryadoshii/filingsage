"""FilingSage dashboard — entrypoint.

    streamlit run app.py          (inside Compose: the `ui` service, port 8501)

Three pages: ask questions with cited answers, browse and add filings, and
watch the ingestion pipeline. The dashboard is only a client of the API
(API_URL); it holds no state of its own beyond the browser session.
"""

import streamlit as st

from filingsage_ui.components import apply_styles

st.set_page_config(
    page_title="FilingSage",
    page_icon=":material/plagiarism:",
    layout="wide",
    initial_sidebar_state="collapsed",
)
apply_styles()

pages = st.navigation(
    [
        st.Page("views/ask.py", title="Ask", icon=":material/chat:", default=True),
        st.Page("views/filings.py", title="Filings", icon=":material/description:"),
        st.Page("views/pipeline.py", title="Pipeline", icon=":material/monitoring:"),
    ],
    position="top",
)
pages.run()
