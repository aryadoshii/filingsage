"""FilingSage dashboard — entrypoint.

    streamlit run app.py          (inside Compose: the `ui` service, port 8501)

Pages: Companies (home: every tracked company with headline numbers), a
research page per company (financials, events, filings, scoped Q&A), Ask
(Q&A across everything), Filings (track companies, browse filings) and
Pipeline (live ingestion view). The dashboard is only a client of the API
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
        st.Page("views/companies.py", title="Companies", icon=":material/domain:", default=True),
        st.Page("views/company.py", title="Company", icon=":material/query_stats:",
                url_path="company"),
        st.Page("views/ask.py", title="Ask", icon=":material/chat:"),
        st.Page("views/filings.py", title="Filings", icon=":material/description:"),
        st.Page("views/pipeline.py", title="Pipeline", icon=":material/monitoring:"),
    ],
    position="top",
)
pages.run()
