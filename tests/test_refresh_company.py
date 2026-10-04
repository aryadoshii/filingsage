"""refresh_company (worker) — profile + 8-K item backfill + XBRL facts, end to
end against a real Postgres (testcontainers) and a faked EDGAR
(httpx.MockTransport). Also checks that ingest_watchlist triggers it."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

import filingsage.db.session as db_session
from filingsage.connectors.edgar import EdgarClient, EdgarConnector
from filingsage.db.models import Company, Event, Filing, FinancialFact
from filingsage.worker import tasks

pytestmark = pytest.mark.integration

CIK = 900001
TICKERS = {"0": {"cik_str": CIK, "ticker": "ACME", "title": "Acme Corp"}}
SUBMISSIONS = {
    "name": "ACME CORP",
    "sicDescription": "Electronic Computers",
    "fiscalYearEnd": "0927",
    "exchanges": ["Nasdaq"],
    "filings": {"recent": {
        "accessionNumber": ["0000900001-25-000002", "0000900001-25-000001"],
        "form": ["10-K", "8-K"],
        "filingDate": ["2025-10-31", "2025-08-01"],
        "primaryDocument": ["k.htm", "e.htm"],
        "items": ["", "2.02,9.01"],
    }},
}
COMPANY_FACTS = {"facts": {"us-gaap": {
    "Revenues": {"units": {"USD": [
        {"start": "2024-09-29", "end": "2025-09-27", "val": 470, "filed": "2025-10-31",
         "form": "10-K", "accn": "0000900001-25-000002"},
    ]}},
}}}


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield create_engine(url)


@pytest.fixture(autouse=True)
def _wire(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "company_tickers" in url:
            return httpx.Response(200, json=TICKERS)
        if "/submissions/" in url:
            return httpx.Response(200, json=SUBMISSIONS)
        if "/api/xbrl/companyfacts/" in url:
            return httpx.Response(200, json=COMPANY_FACTS)
        return httpx.Response(404)

    client = EdgarClient(
        contact_email="arya@test.dev", max_per_second=10_000,
        transport=httpx.MockTransport(handler), sleep=lambda _: None,
    )
    connector = EdgarConnector(client, bronze_dir=tmp_path / "bronze")
    monkeypatch.setattr(tasks, "_connector", lambda: connector)


def _seed_company_with_an_old_8k():
    with db_session.session_scope() as s:
        if s.get(Company, CIK) is None:
            s.add(Company(cik=CIK, ticker="ACME", name="Acme Corp"))
            s.flush()
            s.add(Filing(cik=CIK, accession_no="0000900001-25-000001", form_type="8-K",
                         filed_at=date(2025, 8, 1), primary_document="e.htm"))


def test_refresh_company_stores_profile_items_and_facts():
    _seed_company_with_an_old_8k()

    result = tasks.refresh_company(CIK)

    assert result == {"cik": CIK, "facts": 1, "items_backfilled": 1}
    with db_session.session_scope() as s:
        company = s.get(Company, CIK)
        assert company.sector == "Electronic Computers"
        assert company.fiscal_year_end == "0927"
        assert company.exchange == "Nasdaq"
        assert company.financials_updated_at is not None
        old_8k = s.scalar(select(Filing).where(Filing.accession_no == "0000900001-25-000001"))
        assert old_8k.items == "2.02,9.01"
        [fact] = s.scalars(select(FinancialFact).where(FinancialFact.cik == CIK)).all()
        assert (fact.metric, fact.period, fact.value) == ("revenue", "annual", 470)
        assert s.scalar(select(Event).where(Event.type == "company.refreshed")) is not None


def test_refreshing_twice_replaces_rather_than_duplicates():
    _seed_company_with_an_old_8k()
    tasks.refresh_company(CIK)
    tasks.refresh_company(CIK)

    with db_session.session_scope() as s:
        assert len(s.scalars(select(FinancialFact).where(FinancialFact.cik == CIK)).all()) == 1


def test_ingest_enqueues_a_refresh_for_new_10k_filings(monkeypatch):
    enqueued: list[int] = []
    monkeypatch.setattr(tasks.refresh_company, "delay", lambda cik: enqueued.append(cik))
    monkeypatch.setattr(tasks.fetch_filing, "delay", lambda accession: None)

    result = tasks.ingest_watchlist(["ACME"], limit=None)

    assert result["inserted"] >= 1  # the 10-K is new
    assert enqueued == [CIK]
    with db_session.session_scope() as s:
        tenk = s.scalar(select(Filing).where(Filing.accession_no == "0000900001-25-000002"))
        assert tenk.items is None  # 10-Ks have no item codes; empty string stored as NULL
