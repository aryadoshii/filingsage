"""Integration tests for the discover -> fetch -> parse -> chunk_and_embed
Celery pipeline.

Real Postgres via testcontainers + real alembic migrations (same pattern as
test_db.py). EDGAR is faked with httpx.MockTransport (no network); Qdrant is
faked with qdrant-client's in-memory mode (never the real Qdrant Cloud
cluster). Celery runs in eager mode (no broker) so `.delay()` inside a task
body executes the next task's function synchronously in-process — which
means every test in this module runs the FULL chain through chunk_and_embed,
not just the steps it's nominally about. `_connector()`/`get_settings()`
(tasks module) and `get_client()` (vector_store module) are the construction
seams — monkeypatched per test so nothing here touches the real network,
the real data/ directory, or a real Qdrant cluster.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from alembic import command
from alembic.config import Config
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

import filingsage.db.session as db_session
import filingsage.gold.vector_store as vector_store
import filingsage.worker.tasks as tasks
from filingsage.connectors.edgar import EdgarClient, EdgarConnector, UnknownTickerError
from filingsage.db.events import emit_event
from filingsage.db.models import Chunk as ChunkRow
from filingsage.db.models import Company, Event, Filing, FilingStatus
from filingsage.worker.retry import MAX_RETRIES

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

# 12 distinct Item sections, each short enough to be its own single chunk —
# 12 chunks total, more than EMBED_BATCH_SIZE (8), so a filing built from
# this actually exercises the multi-batch embed/upsert path.
MANY_CHUNKS_8K = b"<html><body>" + b"".join(
    (
        f"<div>Item {n}.01. Custom Disclosure {n}.</div>"
        f"<p>This is unique disclosure content number {n} for testing multi-batch "
        f"embedding, filed as part of a routine update to shareholders.</p>"
    ).encode()
    for n in range(1, 13)
) + b"</body></html>"


class _FlakyClient:
    """Proxies a real QdrantClient; raises on the Nth call to .upsert(),
    succeeds on every other call — see tests/test_vector_store.py for the
    unit-level version of this same fake.
    """

    def __init__(self, inner, fail_at_call: int):
        self._inner = inner
        self._fail_at_call = fail_at_call
        self.upsert_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def upsert(self, *args, **kwargs):
        self.upsert_calls += 1
        if self.upsert_calls == self._fail_at_call:
            raise RuntimeError("simulated Qdrant upsert failure")
        return self._inner.upsert(*args, **kwargs)


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
    """MockTransport handler: ticker map + submissions + archives, by accession.

    `archive_failures` are served, in order, for the first document
    requests — an exception is raised (a transport error), a Response is
    returned — before documents are served normally.
    """

    def __init__(self, submissions: dict, filing_bytes: dict[str, bytes], archive_failures=()):
        self.requests: list[httpx.Request] = []
        self._submissions = submissions
        self._filing_bytes = filing_bytes
        self._archive_failures = list(archive_failures)

    @property
    def archive_requests(self) -> int:
        return sum("/Archives/" in str(r.url) for r in self.requests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "company_tickers" in url:
            return httpx.Response(200, json=TICKER_FIXTURE)
        if "/submissions/" in url:
            return httpx.Response(200, json=self._submissions)
        if "/Archives/" in url and self._archive_failures:
            failure = self._archive_failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        if "/Archives/" in url:
            for accession, content in self._filing_bytes.items():
                if accession.replace("-", "") in url:
                    return httpx.Response(200, content=content)
        return httpx.Response(404)


def _make_connector(tmp_path, submissions, filing_bytes, archive_failures=()):
    handler = Handler(submissions, filing_bytes, archive_failures)
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


@pytest.fixture(autouse=True)
def _wire_vector_store(monkeypatch):
    """Point vector_store.get_client() at in-memory Qdrant, not the real cluster.

    Autouse: eager Celery mode means EVERY test's chain now cascades all the
    way through chunk_and_embed (there's no way to stop it at an earlier
    step short of not calling .delay() at all), so even tests that aren't
    nominally about embeddings would otherwise hit filingsage.config's
    empty-string QDRANT_URL default.
    """
    client = QdrantClient(":memory:")
    monkeypatch.setattr(vector_store, "get_client", lambda: client)


@pytest.fixture
def wire_connector(tmp_path, monkeypatch):
    """Wire tasks._connector()/get_settings() to a fake EDGAR + tmp data dir."""

    def _wire(submissions, filing_bytes, archive_failures=()):
        connector, handler = _make_connector(tmp_path, submissions, filing_bytes, archive_failures)
        monkeypatch.setattr(tasks, "_connector", lambda: connector)
        monkeypatch.setattr(tasks, "get_settings", lambda: _FakeSettings(tmp_path))
        return connector, handler

    return _wire


def test_full_chain_reaches_embedded_with_events(wire_connector):
    accession_no = "0000900001-26-000001"
    submissions = _submissions([accession_no], ["8-K"], ["2026-06-01"], ["a.htm"])
    wire_connector(submissions, {accession_no: HAPPY_8K})

    result = tasks.ingest_watchlist(["ACME"], limit=None)
    assert result == {"discovered": 1, "inserted": 1}

    with db_session.session_scope() as session:
        filing = session.scalar(select(Filing).where(Filing.accession_no == accession_no))
        assert filing.status == FilingStatus.EMBEDDED.value
        assert filing.r2_bronze_key is not None
        assert filing.r2_silver_key is not None

        events = {
            e.type: e
            for e in session.scalars(
                select(Event).where(Event.entity_id == accession_no)
            ).all()
        }
        assert set(events) == {
            "filing.discovered",
            "filing.fetched",
            "filing.parsed",
            "filing.embedded",
        }

        chunk_rows = session.scalars(
            select(ChunkRow).where(ChunkRow.filing_id == filing.id)
        ).all()
        assert len(chunk_rows) > 0
        assert events["filing.embedded"].payload_json == {"chunk_count": len(chunk_rows)}
        assert all(row.qdrant_point_id is not None for row in chunk_rows)

        # points actually landed in the (in-memory) vector store, not just DB rows
        count = vector_store.get_client().count(vector_store.COLLECTION_NAME, exact=True).count
        assert count == len(chunk_rows)


def test_full_chain_batches_embeddings_for_a_filing_with_many_chunks(wire_connector):
    accession_no = "0000900001-26-000010"
    submissions = _submissions([accession_no], ["8-K"], ["2026-06-10"], ["a.htm"])
    wire_connector(submissions, {accession_no: MANY_CHUNKS_8K})

    tasks.ingest_watchlist(["ACME"], limit=None)

    with db_session.session_scope() as session:
        filing = session.scalar(select(Filing).where(Filing.accession_no == accession_no))
        assert filing.status == FilingStatus.EMBEDDED.value

        chunk_rows = session.scalars(
            select(ChunkRow).where(ChunkRow.filing_id == filing.id)
        ).all()
        # 12 sections built into MANY_CHUNKS_8K, more than EMBED_BATCH_SIZE —
        # every one of them must have embedded/upserted, not just the first batch.
        assert len(chunk_rows) == 12
        assert all(row.qdrant_point_id is not None for row in chunk_rows)

        count = vector_store.get_client().count(vector_store.COLLECTION_NAME, exact=True).count
        assert count == 12


def test_qdrant_failure_on_a_later_batch_leaves_status_not_embedded(wire_connector, monkeypatch):
    accession_no = "0000900001-26-000011"
    submissions = _submissions([accession_no], ["8-K"], ["2026-06-11"], ["a.htm"])
    wire_connector(submissions, {accession_no: MANY_CHUNKS_8K})

    # Fail the 2nd .upsert() call: the 1st batch (EMBED_BATCH_SIZE chunks)
    # succeeds, forcing an actual partial-batch failure, not just a
    # first-call failure.
    real_client = vector_store.get_client()
    flaky = _FlakyClient(real_client, fail_at_call=2)
    monkeypatch.setattr(vector_store, "get_client", lambda: flaky)

    with pytest.raises(RuntimeError, match="simulated Qdrant upsert failure"):
        tasks.ingest_watchlist(["ACME"], limit=None)

    with db_session.session_scope() as session:
        filing = session.scalar(select(Filing).where(Filing.accession_no == accession_no))
        # Qdrant-before-status ordering held: the whole chunk_and_embed
        # transaction rolled back, so status never advanced past PARSED —
        # safely retryable, nothing left half-done in Postgres.
        assert filing.status == FilingStatus.PARSED.value

        embedded_event = session.scalar(
            select(Event).where(
                Event.entity_id == accession_no, Event.type == "filing.embedded"
            )
        )
        assert embedded_event is None

        # The DB transaction rolled back entirely, so chunk rows from this
        # failed run are gone too — a retry starts clean, not half-persisted.
        chunk_rows = session.scalars(
            select(ChunkRow).where(ChunkRow.filing_id == filing.id)
        ).all()
        assert chunk_rows == []

    # ...and the failure itself is on record, in its own transaction, which
    # the rollback above didn't take with it. RuntimeError isn't transient,
    # so there was exactly one attempt.
    assert _payloads(accession_no) == [
        {"step": "embed", "error": "RuntimeError: simulated Qdrant upsert failure", "attempts": 1}
    ]


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


# --- retries and failure events (decision #37) --------------------------------


def _payloads(entity_id: str, event_type: str = "filing.failed") -> list[dict]:
    """Payloads of one entity's events of one type, oldest first."""
    with db_session.session_scope() as session:
        return [
            e.payload_json
            for e in session.scalars(
                select(Event)
                .where(Event.entity_id == entity_id, Event.type == event_type)
                .order_by(Event.id)
            )
        ]


def _status(accession_no: str) -> str:
    with db_session.session_scope() as session:
        return session.scalar(select(Filing.status).where(Filing.accession_no == accession_no))


@pytest.fixture
def eager_retries(monkeypatch):
    """Let Celery's eager mode actually run retries. With
    task_eager_propagates on (this module's default) the tracer re-raises
    Celery's Retry exception to the caller instead of re-running the task;
    off, apply() re-runs it with retries + 1 — what a worker does after the
    countdown. A final failure then lands as a FAILURE result rather than an
    exception in the test, so these tests assert on events, not raises."""
    monkeypatch.setitem(tasks.celery_app.conf, "task_eager_propagates", False)


def test_a_transient_fetch_error_is_retried_and_the_chain_completes(wire_connector, eager_retries):
    accession_no = "0000900001-26-000020"
    _, handler = wire_connector(
        _submissions([accession_no], ["8-K"], ["2026-06-20"], ["a.htm"]),
        {accession_no: HAPPY_8K},
        archive_failures=[httpx.ConnectError("connection reset by peer")],
    )

    tasks.ingest_watchlist(["ACME"], limit=None)

    assert handler.archive_requests == 2  # failed once, retried once
    assert _status(accession_no) == FilingStatus.EMBEDDED.value
    assert _payloads(accession_no) == []  # a recovered retry isn't a failure


def test_a_fetch_that_keeps_failing_records_one_event_after_the_last_retry(
    wire_connector, eager_retries
):
    accession_no = "0000900001-26-000021"
    _, handler = wire_connector(
        _submissions([accession_no], ["8-K"], ["2026-06-21"], ["a.htm"]),
        {accession_no: HAPPY_8K},
        archive_failures=[httpx.ConnectError("network is unreachable")] * 10,
    )

    tasks.ingest_watchlist(["ACME"], limit=None)

    assert handler.archive_requests == MAX_RETRIES + 1
    assert _payloads(accession_no) == [
        {"step": "fetch", "error": "ConnectError: network is unreachable", "attempts": MAX_RETRIES + 1}
    ]
    assert _status(accession_no) == FilingStatus.DISCOVERED.value  # left for the reconciler


def test_a_non_transient_fetch_error_fails_once_without_retrying(wire_connector):
    accession_no = "0000900001-26-000022"
    _, handler = wire_connector(
        _submissions([accession_no], ["8-K"], ["2026-06-22"], ["a.htm"]),
        {accession_no: HAPPY_8K},
        archive_failures=[httpx.Response(404)],
    )

    with pytest.raises(httpx.HTTPStatusError):
        tasks.ingest_watchlist(["ACME"], limit=None)

    assert handler.archive_requests == 1
    [failure] = _payloads(accession_no)
    assert (failure["step"], failure["attempts"]) == ("fetch", 1)
    assert failure["error"].startswith("HTTPStatusError: Client error '404 Not Found'")


def test_a_parse_crash_is_recorded_without_touching_the_status(wire_connector, tmp_path):
    """Bronze gone from disk: FileNotFoundError is deterministic, so one
    attempt, one failure event, and the filing stays FETCHED for the
    reconciler to reset."""
    wire_connector(_submissions([], [], [], []), {})
    accession_no = "0000900001-26-000023"
    with db_session.session_scope() as session:
        if session.get(Company, 900001) is None:
            session.add(Company(cik=900001, ticker="ACME", name="Acme Corp"))
            session.flush()
        session.add(Filing(
            cik=900001, accession_no=accession_no, form_type="8-K", filed_at=date(2026, 6, 23),
            primary_document="a.htm", status=FilingStatus.FETCHED.value,
            r2_bronze_key=str(tmp_path / "missing.htm"),
        ))

    with pytest.raises(FileNotFoundError):
        tasks.parse_filing.delay(accession_no)

    [failure] = _payloads(accession_no)
    assert (failure["step"], failure["attempts"]) == ("parse", 1)
    assert failure["error"].startswith("FileNotFoundError:")
    assert _status(accession_no) == FilingStatus.FETCHED.value


def test_a_discovery_failure_records_an_ingest_failed_event(wire_connector):
    wire_connector(_submissions([], [], [], []), {})

    with pytest.raises(UnknownTickerError):
        tasks.ingest_watchlist(["NOPE"], limit=None)

    last = _payloads("watchlist", "ingest.failed")[-1]
    assert (last["step"], last["attempts"], last["tickers"]) == ("ingest", 1, 1)
    assert last["error"].startswith("UnknownTickerError:")


def test_every_ingest_run_leaves_a_heartbeat_even_with_nothing_new(wire_connector):
    """ingest.completed on every run is what lets /stats tell "EDGAR had
    nothing new" apart from "the scheduler stopped"."""
    accession_no = "0000900001-26-000030"
    wire_connector(
        _submissions([accession_no], ["8-K"], ["2026-06-30"], ["a.htm"]), {accession_no: HAPPY_8K}
    )

    tasks.ingest_watchlist(["ACME"], limit=None)
    tasks.ingest_watchlist(["ACME"], limit=None)  # nothing new the second time

    assert _payloads("watchlist", "ingest.completed")[-2:] == [
        {"tickers": 1, "discovered": 1, "inserted": 1},
        {"tickers": 1, "discovered": 1, "inserted": 0},
    ]
