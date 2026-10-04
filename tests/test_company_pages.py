"""Company research endpoints: /overview, /companies/{ticker},
/companies/{ticker}/financials and /companies/{ticker}/events. Real Postgres
via testcontainers, seeded with facts as refresh_company would store them."""

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
from filingsage.db.models import Company, Filing, FinancialFact

pytestmark = pytest.mark.integration

client = TestClient(app)
CIK = 320193

# Two fiscal years of quarterly revenue + net income, a year-ago quarter for
# growth, and a balance-sheet date matching the latest quarter.
QUARTERS = [
    (date(2024, 6, 30), date(2024, 9, 28), 100.0, 20.0, False),
    (date(2024, 9, 29), date(2024, 12, 28), 104.0, 26.0, False),
    (date(2024, 12, 29), date(2025, 3, 29), 110.0, 22.0, False),
    (date(2025, 3, 30), date(2025, 6, 28), 120.0, 24.0, False),
    (date(2025, 6, 29), date(2025, 9, 27), 136.0, 28.0, True),  # Q4, derived
]


def _fact(metric, period, start, end, value, derived=False, unit="USD"):
    return FinancialFact(
        cik=CIK, metric=metric, period=period, start=start, end=end, value=value,
        unit=unit, derived=derived, concept="test", accession_no=None, filed=None,
    )


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        engine = create_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as s, s.begin():
            s.add(Company(cik=CIK, ticker="AAPL", name="Apple Inc.",
                          sector="Electronic Computers", fiscal_year_end="0927",
                          exchange="Nasdaq"))
            s.add(Company(cik=1, ticker="NEWCO", name="New Co"))  # tracked, no facts yet
            s.flush()
            for start, end, rev, ni, derived in QUARTERS:
                s.add(_fact("revenue", "quarter", start, end, rev, derived))
                s.add(_fact("net_income", "quarter", start, end, ni, derived))
            s.add(_fact("revenue", "annual", date(2024, 9, 29), date(2025, 9, 27), 470.0))
            s.add(_fact("cash", "instant", None, date(2025, 9, 27), 61.0))
            s.add_all([
                Filing(cik=CIK, accession_no="0000320193-25-000071", form_type="8-K",
                       filed_at=date(2025, 10, 30), primary_document="e.htm",
                       items="2.02,9.01"),
                Filing(cik=CIK, accession_no="0000320193-25-000050", form_type="8-K",
                       filed_at=date(2025, 6, 1), primary_document="f.htm", items="4.02"),
                Filing(cik=CIK, accession_no="0000320193-25-000040", form_type="8-K",
                       filed_at=date(2025, 5, 1), primary_document="g.htm"),  # no items yet
                Filing(cik=CIK, accession_no="0000320193-25-000079", form_type="10-K",
                       filed_at=date(2025, 10, 31), primary_document="k.htm"),
            ])
        yield engine


@pytest.fixture(autouse=True)
def _wire(engine, monkeypatch):
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )


def test_company_detail_includes_profile_and_key_stats():
    body = client.get("/companies/aapl").json()

    assert (body["ticker"], body["sector"], body["exchange"]) == (
        "AAPL", "Electronic Computers", "Nasdaq")
    stats = {s["key"]: s for s in body["key_stats"]}
    assert stats["revenue_ttm"]["value"] == 104 + 110 + 120 + 136
    assert stats["revenue_growth_yoy"]["value"] == pytest.approx(0.36)
    assert stats["net_margin_ttm"]["value"] == pytest.approx(100 / 470)
    assert stats["cash"]["value"] == 61


def test_unknown_company_is_a_404_with_a_way_forward():
    resp = client.get("/companies/ZZZZ")
    assert resp.status_code == 404
    assert "Filings page" in resp.json()["detail"]


def test_quarterly_financials_are_oldest_first_and_flag_derived_values():
    body = client.get("/companies/AAPL/financials", params={"limit": 4}).json()

    cols = body["columns"]
    assert body["period"] == "quarter"
    assert [c["end"] for c in cols] == ["2024-12-28", "2025-03-29", "2025-06-28", "2025-09-27"]
    assert cols[-1]["values"]["revenue"] == 136
    assert cols[-1]["values"]["cash"] == 61
    assert set(cols[-1]["derived"]) == {"revenue", "net_income"}
    assert cols[0]["derived"] == []


def test_annual_financials():
    cols = client.get("/companies/AAPL/financials", params={"period": "annual"}).json()["columns"]
    assert [c["values"]["revenue"] for c in cols] == [470]


def test_a_company_without_facts_gets_an_empty_statement_not_an_error():
    body = client.get("/companies/NEWCO/financials").json()
    assert body["columns"] == []
    stats = {s["key"]: s["value"] for s in client.get("/companies/NEWCO").json()["key_stats"]}
    assert all(v is None for v in stats.values())


def test_events_turn_8k_item_codes_into_plain_language():
    events = client.get("/companies/AAPL/events").json()

    assert [e["filed_at"] for e in events] == ["2025-10-30", "2025-06-01", "2025-05-01"]
    earnings, restatement, unknown = events
    assert earnings["events"] == ["Released earnings results"]  # 9.01 exhibits hidden
    assert not earnings["notable"]
    assert restatement["notable"]
    assert unknown["events"] == ["Current report"]  # items not backfilled yet


def test_overview_lists_every_company_with_headline_numbers():
    rows = {r["ticker"]: r for r in client.get("/overview").json()}

    assert rows["AAPL"]["revenue_ttm"] == 470
    assert rows["AAPL"]["revenue_growth_yoy"] == pytest.approx(0.36)
    assert rows["AAPL"]["sector"] == "Electronic Computers"
    assert rows["NEWCO"]["revenue_ttm"] is None
