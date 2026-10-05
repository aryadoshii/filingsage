"""The pipeline reconciler (worker/recovery.py, decision #37) — real Postgres
via testcontainers, real files on disk; each task's .delay is replaced with
a recorder, so nothing reaches a broker.

One container for the module, emptied before each test: the reconciler
looks at every filing in the database, so tests must not see each other's
rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

import filingsage.db.session as db_session
import filingsage.worker.recovery as recovery
from filingsage.db.events import emit_event
from filingsage.db.models import Company, Event, Filing, FilingStatus

pytestmark = pytest.mark.integration

CIK = 900001
LONG_AGO = datetime.now(UTC) - timedelta(hours=2)


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield create_engine(url)


@pytest.fixture(autouse=True)
def _fresh_database(engine, monkeypatch):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE events, chunks, filings, companies RESTART IDENTITY CASCADE"))
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    with db_session.session_scope() as session:
        session.add(Company(cik=CIK, ticker="ACME", name="Acme Corp"))


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple[str, str]]:
    """Every .delay() the reconciler makes, as (task, accession_no)."""
    calls: list[tuple[str, str]] = []
    for name in ("fetch_filing", "parse_filing", "chunk_and_embed"):
        task = getattr(recovery, name)
        monkeypatch.setattr(task, "delay", lambda acc, name=name: calls.append((name, acc)))
    return calls


def _filing(
    tmp_path,
    accession_no: str,
    status: str,
    *,
    updated_at: datetime = LONG_AGO,
    bronze: bool = True,
    silver: bool = True,
) -> None:
    """A filing in `status`, last updated at `updated_at`, whose bronze and
    silver files exist on disk (or don't) — as its status implies."""
    bronze_key = silver_key = None
    if status in (FilingStatus.FETCHED.value, FilingStatus.PARSED.value):
        bronze_key = str(tmp_path / f"{accession_no}.htm")
        if bronze:
            (tmp_path / f"{accession_no}.htm").write_text("<html>bronze</html>")
    if status == FilingStatus.PARSED.value:
        silver_key = str(tmp_path / f"{accession_no}.parquet")
        if silver:
            (tmp_path / f"{accession_no}.parquet").write_bytes(b"silver")
    with db_session.session_scope() as session:
        session.add(Filing(
            cik=CIK, accession_no=accession_no, form_type="8-K", filed_at=date(2026, 6, 1),
            primary_document="a.htm", status=status,
            r2_bronze_key=bronze_key, r2_silver_key=silver_key,
        ))
    with db_session.session_scope() as session:  # explicit value beats onupdate=now()
        session.execute(
            update(Filing).where(Filing.accession_no == accession_no).values(updated_at=updated_at)
        )


def _fail(accession_no: str, times: int) -> None:
    with db_session.session_scope() as session:
        for _ in range(times):
            emit_event(session, "filing.failed", accession_no,
                       {"step": "embed", "error": "RuntimeError: boom", "attempts": 1})


def _events(event_type: str) -> list[Event]:
    with db_session.session_scope() as session:
        return list(session.scalars(select(Event).where(Event.type == event_type).order_by(Event.id)))


def _filing_row(accession_no: str) -> Filing:
    with db_session.session_scope() as session:
        return session.scalar(select(Filing).where(Filing.accession_no == accession_no))


def test_each_stuck_status_gets_the_step_that_moves_it_forward(tmp_path, enqueued):
    _filing(tmp_path, "acc-discovered", FilingStatus.DISCOVERED.value)
    _filing(tmp_path, "acc-fetched", FilingStatus.FETCHED.value)
    _filing(tmp_path, "acc-parsed", FilingStatus.PARSED.value)

    result = recovery.reconcile_stuck_filings()

    assert sorted(enqueued) == [
        ("chunk_and_embed", "acc-parsed"),
        ("fetch_filing", "acc-discovered"),
        ("parse_filing", "acc-fetched"),
    ]
    assert result.reset == []
    requeued = {e.entity_id: e.payload_json for e in _events("filing.requeued")}
    assert requeued["acc-parsed"] == {"from_status": "parsed"}
    # Re-queued counts as progress: the next run (30 min later) must not
    # re-queue it again while it may still be waiting in the queue.
    assert _filing_row("acc-parsed").updated_at > LONG_AGO + timedelta(hours=1)


@pytest.mark.parametrize(
    "status, missing, reason",
    [
        (FilingStatus.FETCHED.value, {"bronze": False}, "bronze missing on disk"),
        (FilingStatus.PARSED.value, {"silver": False}, "silver missing on disk"),
    ],
)
def test_a_missing_input_file_resets_the_filing_to_discovered(tmp_path, enqueued, status, missing, reason):
    _filing(tmp_path, "acc-1", status, **missing)

    result = recovery.reconcile_stuck_filings()

    assert enqueued == [("fetch_filing", "acc-1")]
    assert result.reset == ["acc-1"]
    assert _filing_row("acc-1").status == FilingStatus.DISCOVERED.value
    [reset] = _events("filing.recovery_reset")
    assert reset.payload_json == {"previous_status": status, "reason": reason}


def test_filings_that_failed_three_times_are_left_for_a_person(tmp_path, enqueued):
    _filing(tmp_path, "acc-hopeless", FilingStatus.PARSED.value)
    _fail("acc-hopeless", 3)
    _filing(tmp_path, "acc-twice", FilingStatus.PARSED.value)
    _fail("acc-twice", 2)  # under the threshold: still retried

    result = recovery.reconcile_stuck_filings()

    assert enqueued == [("chunk_and_embed", "acc-twice")]
    assert result.needs_attention == 1
    assert _filing_row("acc-hopeless").updated_at == pytest.approx(LONG_AGO, abs=timedelta(seconds=1))


def test_filings_still_making_progress_are_untouched(tmp_path, enqueued):
    recent = datetime.now(UTC) - timedelta(minutes=5)
    _filing(tmp_path, "acc-fresh", FilingStatus.PARSED.value, updated_at=recent)
    _filing(tmp_path, "acc-done", FilingStatus.EMBEDDED.value)
    _filing(tmp_path, "acc-bad", FilingStatus.QUARANTINED.value)

    result = recovery.reconcile_stuck_filings()

    assert enqueued == []
    assert result.requeued == []
    assert _events("filing.requeued") == []


def test_one_run_requeues_at_most_the_cap_oldest_first(tmp_path, enqueued):
    for i in range(recovery.RECONCILE_LIMIT + 5):
        _filing(tmp_path, f"acc-{i:02d}", FilingStatus.DISCOVERED.value,
                updated_at=LONG_AGO - timedelta(minutes=i))

    result = recovery.reconcile_stuck_filings()

    assert len(enqueued) == recovery.RECONCILE_LIMIT
    assert result.remaining == 5
    oldest_first = sorted((f"acc-{i:02d}" for i in range(30)), reverse=True)
    assert [acc for _, acc in enqueued] == oldest_first[: recovery.RECONCILE_LIMIT]


def test_every_run_leaves_a_summary_even_when_nothing_was_stuck(enqueued):
    result = recovery.reconcile_stuck_filings()

    assert result.counts() == {"requeued": 0, "reset": 0, "needs_attention": 0, "remaining": 0}
    [summary] = _events("pipeline.reconciled")
    assert (summary.entity_id, summary.payload_json) == ("pipeline", result.counts())


def test_the_beat_task_returns_the_run_counts(tmp_path, enqueued):
    _filing(tmp_path, "acc-1", FilingStatus.DISCOVERED.value)
    assert recovery.reconcile_pipeline() == {
        "requeued": 1, "reset": 0, "needs_attention": 0, "remaining": 0,
    }
