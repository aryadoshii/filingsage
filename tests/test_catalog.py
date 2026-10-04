"""Read-side queries (db/queries.py) and the dashboard's API routes
(api/catalog.py). Real Postgres via testcontainers + real alembic
migrations, same pattern as test_pipeline.py; one module-scoped database
seeded once with two companies, three filings, chunks and events.
"""

from __future__ import annotations

from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

import filingsage.db.session as db_session
from filingsage.api.main import app
from filingsage.db import queries
from filingsage.db.events import emit_event
from filingsage.db.models import Chunk, Company, Filing, FilingStatus

pytestmark = pytest.mark.integration

client = TestClient(app)


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        engine = create_engine(url)
        _seed(sessionmaker(bind=engine, expire_on_commit=False))
        yield engine


@pytest.fixture(autouse=True)
def _wire_session_scope(engine, monkeypatch):
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )


def _seed(factory) -> None:
    with factory() as session, session.begin():
        session.add_all([
            Company(cik=320193, ticker="AAPL", name="Apple Inc."),
            Company(cik=789019, ticker="MSFT", name="Microsoft Corp"),
        ])
        session.flush()
        k10 = Filing(
            cik=320193, accession_no="0000320193-25-000079", form_type="10-K",
            filed_at=date(2025, 10, 31), primary_document="aapl-20250927.htm",
            status=FilingStatus.EMBEDDED.value,
        )
        k8 = Filing(
            cik=320193, accession_no="0000320193-26-000010", form_type="8-K",
            filed_at=date(2026, 1, 29), primary_document="aapl-8k.htm",
            status=FilingStatus.PARSED.value,
        )
        msft = Filing(
            cik=789019, accession_no="0000950170-26-000001", form_type="10-Q",
            filed_at=date(2026, 4, 28), primary_document="msft-10q.htm",
            status=FilingStatus.FETCHED.value,
        )
        session.add_all([k10, k8, msft])
        session.flush()
        session.add_all([
            Chunk(filing_id=k10.id, section="risk_factors", seq=0, text="Competition is intense.",
                  text_hash="h0", char_count=23, token_count=4, qdrant_point_id="p0"),
            Chunk(filing_id=k10.id, section="mdna", seq=1, text="Net sales increased.",
                  text_hash="h1", char_count=20, token_count=4, qdrant_point_id="p1"),
            Chunk(filing_id=k8.id, section="item_2_02", seq=0, text="Results of operations.",
                  text_hash="h2", char_count=22, token_count=4),
        ])
        emit_event(session, "filing.discovered", k10.accession_no, {"ticker": "AAPL"})
        emit_event(session, "filing.embedded", k10.accession_no, {"chunk_count": 2})


def test_edgar_url_strips_accession_dashes_and_cik_padding():
    url = queries.edgar_document_url(320193, "0000320193-25-000079", "aapl-20250927.htm")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )


def test_companies_report_filing_and_embedded_counts():
    resp = client.get("/companies")

    assert resp.status_code == 200
    by_ticker = {c["ticker"]: c for c in resp.json()}
    assert by_ticker["AAPL"]["filings"] == 2
    assert by_ticker["AAPL"]["embedded"] == 1
    assert by_ticker["AAPL"]["latest_filed_at"] == "2026-01-29"
    assert by_ticker["MSFT"]["embedded"] == 0


def test_filings_are_newest_first_with_chunk_counts_and_links():
    resp = client.get("/filings")

    rows = resp.json()
    assert [r["form_type"] for r in rows] == ["10-Q", "8-K", "10-K"]
    k10 = rows[-1]
    assert k10["chunk_count"] == 2
    assert k10["edgar_url"].endswith("/320193/000032019325000079/aapl-20250927.htm")
    assert rows[0]["chunk_count"] == 0  # a filing with no chunks yet reports 0, not missing


def test_filings_filters_combine_and_ticker_is_case_insensitive():
    rows = client.get("/filings", params={"ticker": "aapl", "status": "embedded"}).json()
    assert [r["accession_no"] for r in rows] == ["0000320193-25-000079"]


def test_filings_limit_is_capped():
    assert client.get("/filings", params={"limit": 10_000}).status_code == 422


def test_stats_summarise_the_pipeline():
    stats = client.get("/stats").json()

    assert stats["filings_by_status"] == {"embedded": 1, "parsed": 1, "fetched": 1}
    assert stats["total_filings"] == 3
    assert stats["companies"] == 2
    assert stats["chunks"] == 3
    assert stats["embedded_chunks"] == 2  # chunks with a Qdrant point id
    assert stats["last_event_at"] is not None


def test_events_are_newest_first():
    events = client.get("/events", params={"limit": 5}).json()
    assert [e["type"] for e in events] == ["filing.embedded", "filing.discovered"]


def test_citations_return_exact_excerpt_and_skip_unknown_ids():
    with db_session.session_scope() as session:
        ids = [c.chunk_id for c in queries.get_citations(session, list(range(1, 100)))]

    resp = client.get("/citations", params={"ids": f"{ids[0]},999999"})

    assert resp.status_code == 200
    [citation] = resp.json()
    assert citation["chunk_id"] == ids[0]
    assert citation["text"] == "Competition is intense."
    assert citation["section"] == "risk_factors"
    assert citation["ticker"] == "AAPL"
    assert citation["edgar_url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")


def test_citations_reject_malformed_or_oversized_id_lists():
    assert client.get("/citations", params={"ids": "1,abc"}).status_code == 422
    too_many = ",".join(str(i) for i in range(1, 60))
    assert client.get("/citations", params={"ids": too_many}).status_code == 422
