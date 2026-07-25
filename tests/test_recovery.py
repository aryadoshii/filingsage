"""Recovery tool tests — real Postgres via testcontainers (same pattern as
test_pipeline.py); fetch_filing.delay is monkeypatched so nothing hits a
real broker, and no real EDGAR/Fly access is needed or used.
"""

from __future__ import annotations

from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

import filingsage.db.session as db_session
import filingsage.worker.recovery as recovery
from filingsage.db.models import Company, Event, Filing, FilingStatus

pytestmark = pytest.mark.integration


@pytest.fixture
def engine():
    # Function-scoped (not module-scoped like test_pipeline.py's) — these
    # tests assert exact sets of accession numbers returned by a query with
    # no accession-number filter of its own (recover_stale_filings looks at
    # EVERY recoverable filing), so a shared container would leak rows
    # across tests. A fresh container per test is slower but avoids that
    # entirely rather than working around it with disjoint accession
    # numbers and partial-membership assertions.
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield create_engine(url)


@pytest.fixture(autouse=True)
def _wire_session_scope(engine, monkeypatch):
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )


@pytest.fixture
def record_delay(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        recovery.fetch_filing, "delay", lambda accession_no: calls.append(accession_no)
    )
    return calls


def _seed(tmp_path, accession_no: str, status: str, bronze_exists: bool | None) -> None:
    """bronze_exists=None means no r2_bronze_key at all (matches 'discovered')."""
    with db_session.session_scope() as session:
        cik = abs(hash(accession_no)) % 900000 + 1000
        session.add(Company(cik=cik, ticker=f"T{cik}"[:12], name="Test Co"))
        r2_bronze_key = None
        if bronze_exists is not None:
            bronze_path = tmp_path / f"{accession_no}.htm"
            if bronze_exists:
                bronze_path.write_text("<html>real bronze</html>")
            r2_bronze_key = str(bronze_path)
        session.add(
            Filing(
                cik=cik,
                accession_no=accession_no,
                form_type="8-K",
                filed_at=date(2026, 1, 1),
                primary_document="a.htm",
                status=status,
                r2_bronze_key=r2_bronze_key,
            )
        )


def test_dry_run_reports_without_mutating_anything(tmp_path, record_delay):
    _seed(tmp_path, "acc-discovered", FilingStatus.DISCOVERED.value, bronze_exists=None)
    _seed(tmp_path, "acc-fetched-missing", FilingStatus.FETCHED.value, bronze_exists=False)
    _seed(tmp_path, "acc-parsed-intact", FilingStatus.PARSED.value, bronze_exists=True)
    _seed(tmp_path, "acc-quarantined", FilingStatus.QUARANTINED.value, bronze_exists=False)
    _seed(tmp_path, "acc-embedded", FilingStatus.EMBEDDED.value, bronze_exists=True)

    plan = recovery.recover_stale_filings(dry_run=True)

    assert set(plan.reset) == {"acc-discovered", "acc-fetched-missing"}
    assert set(plan.intact) == {"acc-parsed-intact"}
    # quarantined/embedded never enter the plan at all
    assert "acc-quarantined" not in plan.reset and "acc-quarantined" not in plan.intact
    assert "acc-embedded" not in plan.reset and "acc-embedded" not in plan.intact

    assert record_delay == []  # no Celery calls in dry-run

    with db_session.session_scope() as session:
        statuses = {
            f.accession_no: f.status
            for f in session.scalars(select(Filing)).all()
        }
    assert statuses["acc-discovered"] == FilingStatus.DISCOVERED.value
    assert statuses["acc-fetched-missing"] == FilingStatus.FETCHED.value  # unchanged
    assert statuses["acc-parsed-intact"] == FilingStatus.PARSED.value  # unchanged

    events = db_session_events()
    assert events == []  # no events emitted in dry-run


def db_session_events() -> list[Event]:
    with db_session.session_scope() as session:
        return list(session.scalars(select(Event)))


def test_real_run_resets_status_emits_events_and_enqueues(tmp_path, record_delay):
    _seed(tmp_path, "acc2-fetched-missing", FilingStatus.FETCHED.value, bronze_exists=False)
    _seed(tmp_path, "acc2-parsed-missing", FilingStatus.PARSED.value, bronze_exists=False)
    _seed(tmp_path, "acc2-parsed-intact", FilingStatus.PARSED.value, bronze_exists=True)

    plan = recovery.recover_stale_filings(
        dry_run=False, batch_size=100, batch_delay_seconds=0, sleep=lambda s: None
    )

    assert set(plan.reset) == {"acc2-fetched-missing", "acc2-parsed-missing"}
    assert set(plan.intact) == {"acc2-parsed-intact"}

    with db_session.session_scope() as session:
        statuses = {
            f.accession_no: f.status
            for f in session.scalars(select(Filing)).all()
            if f.accession_no.startswith("acc2-")
        }
    assert statuses["acc2-fetched-missing"] == FilingStatus.DISCOVERED.value
    assert statuses["acc2-parsed-missing"] == FilingStatus.DISCOVERED.value
    assert statuses["acc2-parsed-intact"] == FilingStatus.PARSED.value  # untouched

    assert set(record_delay) == {"acc2-fetched-missing", "acc2-parsed-missing"}
    assert "acc2-parsed-intact" not in record_delay

    with db_session.session_scope() as session:
        reset_events = list(
            session.scalars(
                select(Event).where(Event.type == "filing.recovery_reset")
            )
        )
    reset_by_accession = {e.entity_id: e for e in reset_events}
    assert reset_by_accession["acc2-fetched-missing"].payload_json == {
        "previous_status": "fetched", "reason": "bronze missing on disk",
    }
    assert reset_by_accession["acc2-parsed-missing"].payload_json == {
        "previous_status": "parsed", "reason": "bronze missing on disk",
    }


def test_batching_sleeps_between_batches_not_after_the_last(tmp_path, record_delay):
    for i in range(5):
        _seed(tmp_path, f"acc3-{i}", FilingStatus.FETCHED.value, bronze_exists=False)

    sleeps: list[float] = []
    recovery.recover_stale_filings(
        dry_run=False, batch_size=2, batch_delay_seconds=7.5, sleep=sleeps.append
    )

    # 5 accessions / batch_size 2 -> batches of [2, 2, 1] -> 2 sleeps between
    # them, none after the final batch.
    assert sleeps == [7.5, 7.5]
    assert len(record_delay) == 5


def test_recover_with_nothing_to_reset_is_a_noop(tmp_path, record_delay):
    _seed(tmp_path, "acc4-intact", FilingStatus.PARSED.value, bronze_exists=True)

    plan = recovery.recover_stale_filings(dry_run=False, sleep=lambda s: None)

    assert plan.reset == []
    assert plan.intact == ["acc4-intact"]
    assert record_delay == []
