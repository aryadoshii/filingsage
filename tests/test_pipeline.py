"""Integration tests for the discover -> fetch -> parse Celery pipeline.

Real Postgres via testcontainers + real alembic migrations (same pattern as
test_db.py). EDGAR is faked with httpx.MockTransport (no network); Celery
runs in eager mode (no broker) so `.delay()` inside a task body executes the
next task's function synchronously in-process. `_connector()` and
`get_settings()` are the tasks module's construction seams — monkeypatched
per test so tasks never touch the real network or the real data/ directory.
"""

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
import filingsage.worker.tasks as tasks
from filingsage.connectors.edgar import EdgarClient, EdgarConnector
from filingsage.db.events import emit_event
from filingsage.db.models import Company, Event, Filing, FilingStatus

pytestmark = pytest.mark.integration

TICKER_FIXTURE = {"0": {"cik_str": 900001, "ticker": "ACME", "title": "Acme Corp"}}

HAPPY_8K = (
    b"<html><body>"
    b"<div>Item 5.02. Officer Changes.</div>"
    b"<p>On June 1, 2026, the Company appointed a new Chief Technology Officer "
    b"to lead engineering, effective immediately, with broad responsibility "
    b"across the organization.</p>"
    b"</body></html>"
)

QUARANTINE_8K = b"<html><body><p>No SEC Item headings anywhere in here.</p></body></html>"


def _submissions(accessions, forms, filed_dates, docs):
    return {
        "cik": "900001",
        "filings": {
            "recent": {
                "accessionNumber": accessions,
                "form": forms,
                "filingDate": filed_dates,
                "primaryDocument": docs,
            }
        },
    }


class Handler:
    """MockTransport handler: ticker map + submissions + archives, by accession."""

    def __init__(self, submissions: dict, filing_bytes: dict[str, bytes]):
        self.requests: list[httpx.Request] = []
        self._submissions = submissions
        self._filing_bytes = filing_bytes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "company_tickers" in url:
            return httpx.Response(200, json=TICKER_FIXTURE)
        if "/submissions/" in url:
            return httpx.Response(200, json=self._submissions)
        if "/Archives/" in url:
            for accession, content in self._filing_bytes.items():
                if accession.replace("-", "") in url:
                    return httpx.Response(200, content=content)
        return httpx.Response(404)


def _make_connector(tmp_path, submissions, filing_bytes):
    handler = Handler(submissions, filing_bytes)
    client = EdgarClient(
        contact_email="arya@test.dev",
        max_per_second=10_000,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    return EdgarConnector(client, bronze_dir=tmp_path / "bronze"), handler


class _FakeSettings:
    def __init__(self, tmp_path):
        self.data_dir = tmp_path / "data"


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")  # test the real migration path, not create_all
        yield create_engine(url)


@pytest.fixture(autouse=True)
def _wire_session_scope(engine, monkeypatch):
    """Point session_scope() at the testcontainers engine, bypassing config/settings."""
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )


@pytest.fixture(scope="module", autouse=True)
def _eager_celery():
    """Run .delay() synchronously in-process — no Redis broker in these tests."""
    tasks.celery_app.conf.task_always_eager = True
    tasks.celery_app.conf.task_eager_propagates = True
    yield
    tasks.celery_app.conf.task_always_eager = False
    tasks.celery_app.conf.task_eager_propagates = False


@pytest.fixture
def wire_connector(tmp_path, monkeypatch):
    """Wire tasks._connector()/get_settings() to a fake EDGAR + tmp data dir."""

    def _wire(submissions, filing_bytes):
        connector, handler = _make_connector(tmp_path, submissions, filing_bytes)
        monkeypatch.setattr(tasks, "_connector", lambda: connector)
        monkeypatch.setattr(tasks, "get_settings", lambda: _FakeSettings(tmp_path))
        return connector, handler

    return _wire


def test_full_chain_reaches_parsed_with_events(wire_connector):
    submissions = _submissions(["0000900001-26-000001"], ["8-K"], ["2026-06-01"], ["a.htm"])
    wire_connector(submissions, {"0000900001-26-000001": HAPPY_8K})

    result = tasks.ingest_watchlist(["ACME"], limit=None)
    assert result == {"discovered": 1, "inserted": 1}

    with db_session.session_scope() as session:
        filing = session.scalar(
            select(Filing).where(Filing.accession_no == "0000900001-26-000001")
        )
        assert filing.status == FilingStatus.PARSED.value
        assert filing.r2_bronze_key is not None
        assert filing.r2_silver_key is not None

        events = session.scalars(
            select(Event).where(Event.entity_id == "0000900001-26-000001")
        ).all()
        assert {e.type for e in events} == {
            "filing.discovered",
            "filing.fetched",
            "filing.parsed",
        }


def test_ingest_is_idempotent_on_rerun(wire_connector):
    submissions = _submissions(["0000900001-26-000002"], ["8-K"], ["2026-06-02"], ["a.htm"])
    wire_connector(submissions, {"0000900001-26-000002": HAPPY_8K})

    first = tasks.ingest_watchlist(["ACME"], limit=None)
    assert first["inserted"] == 1

    second = tasks.ingest_watchlist(["ACME"], limit=None)
    assert second["inserted"] == 0  # dedupe gate: already-known accession, no-op

    with db_session.session_scope() as session:
        events = session.scalars(
            select(Event).where(
                Event.entity_id == "0000900001-26-000002",
                Event.type == "filing.discovered",
            )
        ).all()
        assert len(events) == 1  # not duplicated on rerun


def test_quarantine_path_sets_status_and_event(wire_connector):
    submissions = _submissions(["0000900001-26-000003"], ["8-K"], ["2026-06-03"], ["a.htm"])
    wire_connector(submissions, {"0000900001-26-000003": QUARANTINE_8K})

    tasks.ingest_watchlist(["ACME"], limit=None)

    with db_session.session_scope() as session:
        filing = session.scalar(
            select(Filing).where(Filing.accession_no == "0000900001-26-000003")
        )
        assert filing.status == FilingStatus.QUARANTINED.value
        assert filing.r2_silver_key is None

        event = session.scalar(
            select(Event).where(
                Event.entity_id == "0000900001-26-000003",
                Event.type == "filing.parse_failed",
            )
        )
        assert event is not None
        assert "no sections" in event.payload_json["reason"]


def test_status_change_and_event_commit_or_rollback_together():
    with db_session.session_scope() as session:
        session.add(Company(cik=999999, ticker="ROLL", name="Rollback Co"))
        session.add(
            Filing(
                cik=999999,
                accession_no="acc-atomic",
                form_type="8-K",
                filed_at=date(2026, 1, 1),
                primary_document="a.htm",
            )
        )

    with pytest.raises(RuntimeError):
        with db_session.session_scope() as session:
            filing = session.scalar(select(Filing).where(Filing.accession_no == "acc-atomic"))
            filing.status = FilingStatus.FETCHED.value
            emit_event(session, "filing.fetched", "acc-atomic", {"path": "/tmp/x"})
            raise RuntimeError("simulated mid-transaction failure")

    with db_session.session_scope() as session:
        filing = session.scalar(select(Filing).where(Filing.accession_no == "acc-atomic"))
        event = session.scalar(select(Event).where(Event.entity_id == "acc-atomic"))
        assert filing.status == FilingStatus.DISCOVERED.value  # status change rolled back
        assert event is None  # event rolled back too — never both-or-neither violated
