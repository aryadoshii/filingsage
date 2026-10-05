"""Celery tasks: the discover -> fetch -> parse -> chunk_and_embed pipeline (spec §3).

Each task takes only an accession_no string (task-argument hygiene): task
payloads stay small and JSON-serializable, and the Filing row — not a stale
snapshot captured at enqueue time — is the source of truth when a task
actually runs.

Chaining is explicit (`.delay()` from inside the previous task) rather than
a Celery `chain()`/`chord()` primitive: each step's DB write must commit
before the next step is enqueued, and each step independently decides
whether to continue (e.g. parse_filing does not re-enqueue on quarantine).

Failure handling (decision #37): a step that fails with a transient error
(worker/retry.py) retries itself with exponential backoff, up to
MAX_RETRIES times. When it gives up — or fails with an error retrying can't
fix — it records a *.failed event in its own transaction and re-raises, so
Celery still marks the task failed. The filing's status is left alone: the
reconciler (worker/recovery.py) decides what happens next. Each step's
try/except wraps only its own work, never the next step's .delay(), so a
failure is always attributed to the step that actually failed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from celery import Task
from celery.utils.log import get_task_logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from filingsage.config import get_settings
from filingsage.connectors import EdgarClient, EdgarConnector, FilingRef
from filingsage.connectors.rate_limit import shared_edgar_limiter
from filingsage.db.events import emit_event
from filingsage.db.models import Chunk as ChunkRow
from filingsage.db.models import Company, Filing, FilingStatus
from filingsage.db.session import session_scope
from filingsage.financials.store import apply_profile, replace_facts
from filingsage.financials.xbrl import extract_facts
from filingsage.gold.chunking import chunk_filing, persist_chunks
from filingsage.gold.vector_store import upsert_chunks
from filingsage.parsing.silver import ParseQuarantineError, parse_to_silver
from filingsage.worker.celery_app import celery_app
from filingsage.worker.retry import MAX_RETRIES, describe_error, is_transient, retry_delay

logger = get_task_logger(__name__)

# Financials change only when a company files a 10-K/10-Q, which triggers a
# refresh immediately (see ingest_watchlist). This interval is just the
# safety net — e.g. a restatement filed as an amendment the watchlist
# doesn't ingest — so it's measured in days, not hours: one companyfacts
# request per company per week is negligible against SEC's rate limit.
FINANCIALS_MAX_AGE = timedelta(days=7)
PERIODIC_FORMS = frozenset({"10-K", "10-Q"})


def _connector() -> EdgarConnector:
    """Construction seam: tests monkeypatch this instead of building a real client.

    The rate limiter is the process-wide shared one (Redis-backed), not a
    fresh per-client limiter: every task builds a new client, and every
    worker process runs tasks, so only a shared budget keeps the combined
    rate under SEC's cap (decision #36).
    """
    settings = get_settings()
    client = EdgarClient(contact_email=settings.sec_contact_email, limiter=shared_edgar_limiter())
    return EdgarConnector(client, bronze_dir=settings.bronze_dir)


def _ref_for(filing: Filing, company: Company) -> FilingRef:
    """Rebuild a FilingRef from DB rows — the connector's input shape."""
    return FilingRef(
        cik=filing.cik,
        ticker=company.ticker,
        company=company.name,
        accession_number=filing.accession_no,
        form_type=filing.form_type,
        filed_at=filing.filed_at,
        primary_document=filing.primary_document,
    )


def _retry_if_transient(task: Task, exc: Exception) -> int:
    """Called from a task's `except` block. Raises Celery's Retry (with
    backoff) when the error is transient and retries remain; otherwise
    returns how many attempts were made, for the failure event.

    A task called as a plain function (the CLI's refresh-company) has no
    broker to re-queue through, so it never retries — it fails on the first
    attempt like any function would, and still records the failure.
    """
    retries = task.request.retries
    if is_transient(exc) and retries < MAX_RETRIES and not task.request.called_directly:
        countdown = retry_delay(retries)
        logger.warning(
            "%s: %s — retry %d/%d in %.0fs",
            task.name, describe_error(exc), retries + 1, MAX_RETRIES, countdown,
        )
        raise task.retry(exc=exc, countdown=countdown)
    return retries + 1


def _record_failure(event_type: str, entity_id: str, payload: dict) -> None:
    """Write a *.failed event in its OWN transaction — the task's own
    transaction has already rolled back. Best-effort: if the database itself
    is what failed, this can't be recorded, and the task's error log is all
    that's left."""
    try:
        with session_scope() as session:
            emit_event(session, event_type, entity_id, payload)
    except Exception:
        logger.exception("couldn't record %s for %s", event_type, entity_id)


def _failure_payload(step: str, exc: Exception, attempts: int) -> dict:
    return {"step": step, "error": describe_error(exc), "attempts": attempts}


def _ticker_for(cik: int) -> str:
    """A company event's entity id is its ticker; fall back to the CIK if the
    database can't say (it may be the very thing that failed)."""
    try:
        with session_scope() as session:
            company = session.get(Company, cik)
            return company.ticker if company else str(cik)
    except Exception:
        return str(cik)


@celery_app.task(name="filingsage.ping")
def ping() -> str:
    """Round-trip smoke test: API container -> Redis -> worker -> Redis -> caller."""
    return "pong"


@celery_app.task(bind=True, name="filingsage.ingest_watchlist", max_retries=MAX_RETRIES)
def ingest_watchlist(self: Task, tickers: list[str], limit: int | None = None) -> dict:
    """Discover filings for `tickers`, insert genuinely-new ones, enqueue fetches.

    The dedupe gate: `INSERT ... ON CONFLICT (accession_no) DO NOTHING
    RETURNING accession_no` inserts nothing and returns nothing for an
    accession we've already seen. Only rows that come back from RETURNING
    are new — those, and only those, get a filing.discovered event and a
    fetch_filing enqueue. This is what makes the periodic cron re-runnable
    for free: run it again with the same watchlist and it's a no-op past
    the first pass.
    """
    try:
        newly_inserted, refresh_ciks, discovered = _discover_and_insert(tickers, limit)
    except Exception as exc:
        attempts = _retry_if_transient(self, exc)
        _record_failure(
            "ingest.failed", "watchlist",
            {**_failure_payload("ingest", exc, attempts), "tickers": len(tickers)},
        )
        raise

    # Enqueue only after the transaction committed — never fetch a filing
    # whose "discovered" row might not actually be in the database.
    for accession_no in newly_inserted:
        fetch_filing.delay(accession_no)
    for cik in sorted(refresh_ciks):
        refresh_company.delay(cik)

    return {"discovered": discovered, "inserted": len(newly_inserted)}


def _discover_and_insert(
    tickers: list[str], limit: int | None
) -> tuple[list[str], set[int], int]:
    """ingest_watchlist's own work: one EDGAR discovery pass and one
    transaction. Returns (new accession numbers, CIKs whose financials need
    a refresh, filings discovered)."""
    connector = _connector()
    refs = connector.discover(tickers)

    by_ticker: dict[str, list[FilingRef]] = {}
    for ref in refs:
        by_ticker.setdefault(ref.ticker, []).append(ref)

    newly_inserted: list[str] = []
    refresh_ciks: set[int] = set()  # companies whose financials need rebuilding
    with session_scope() as session:
        for rows in by_ticker.values():
            selected = rows[:limit] if limit is not None else rows
            for ref in selected:
                session.execute(
                    pg_insert(Company)
                    .values(cik=ref.cik, ticker=ref.ticker, name=ref.company)
                    .on_conflict_do_update(
                        index_elements=["cik"], set_={"name": ref.company}
                    )
                )
                result = session.execute(
                    pg_insert(Filing)
                    .values(
                        cik=ref.cik,
                        accession_no=ref.accession_number,
                        form_type=ref.form_type,
                        filed_at=ref.filed_at,
                        primary_document=ref.primary_document,
                        items=ref.items or None,
                        status=FilingStatus.DISCOVERED.value,
                    )
                    .on_conflict_do_nothing(index_elements=["accession_no"])
                    .returning(Filing.accession_no)
                )
                if result.first() is None:
                    continue  # already known — dedupe gate, skip silently
                newly_inserted.append(ref.accession_number)
                if ref.form_type in PERIODIC_FORMS:
                    refresh_ciks.add(ref.cik)
                emit_event(
                    session,
                    "filing.discovered",
                    ref.accession_number,
                    {"ticker": ref.ticker, "form_type": ref.form_type},
                )

        # Also refresh any company whose financials were never fetched or
        # have gone stale — the safety net behind the new-10-K/10-Q trigger.
        stale_before = datetime.now(UTC) - FINANCIALS_MAX_AGE
        watched = {ref.cik for ref in refs}
        for company in session.scalars(select(Company).where(Company.cik.in_(watched))):
            if company.financials_updated_at is None or company.financials_updated_at < stale_before:
                refresh_ciks.add(company.cik)

    return newly_inserted, refresh_ciks, len(refs)


@celery_app.task(bind=True, name="filingsage.refresh_company", max_retries=MAX_RETRIES)
def refresh_company(self: Task, cik: int) -> dict:
    """Rebuild one company's profile and financial facts from EDGAR.

    Two requests (submissions + XBRL companyfacts), both made BEFORE the
    transaction opens, so no database connection is held across network
    calls. The facts are then replaced wholesale in one transaction with a
    company.refreshed event — readers see the old set or the new set, never
    a half-written mix.
    """
    try:
        connector = _connector()
        profile = connector.profile(cik)
        document = connector.company_facts(cik)
        facts = extract_facts(document) if document else []

        with session_scope() as session:
            company = session.get(Company, cik)
            if company is None:
                logger.warning("refresh_company: unknown cik %s", cik)
                return {"cik": cik, "facts": 0}
            backfilled = apply_profile(session, company, profile)
            fact_count = replace_facts(session, cik, facts)
            company.financials_updated_at = datetime.now(UTC)
            emit_event(
                session, "company.refreshed", company.ticker,
                {"facts": fact_count, "items_backfilled": backfilled},
            )
    except Exception as exc:
        attempts = _retry_if_transient(self, exc)
        _record_failure(
            "company.refresh_failed", _ticker_for(cik),
            {**_failure_payload("refresh", exc, attempts), "cik": cik},
        )
        raise
    return {"cik": cik, "facts": fact_count, "items_backfilled": backfilled}


@celery_app.task(name="filingsage.scheduled_ingest")
def scheduled_ingest() -> dict:
    """Celery beat's entrypoint (celery_app.beat_schedule) — every 2h.

    Resolves the ticker list when it RUNS, not when beat starts: beat
    pickles a schedule entry's arguments once, so a hard-coded ticker list
    there would go stale the moment it changes. Today the list is the
    configured default universe; once watchlists exist it becomes their
    union, and only this function changes.

    Enqueues ingest_watchlist rather than calling it in-process: a task
    called as a plain function has no broker to retry through, so the
    scheduled run — the one that matters most — would get none of
    ingest_watchlist's retry-with-backoff policy. One extra queue hop buys it.
    """
    settings = get_settings()
    result = ingest_watchlist.delay(settings.default_universe, settings.ingest_limit_per_ticker)
    return {"task_id": result.id}


@celery_app.task(bind=True, name="filingsage.fetch_filing", max_retries=MAX_RETRIES)
def fetch_filing(self: Task, accession_no: str) -> None:
    """Fetch one filing's primary document into bronze; enqueue parse_filing.

    Status change, bronze key, and the filing.fetched event all commit in one
    session_scope — a crash mid-task can never leave the DB claiming FETCHED
    without a bronze file, or vice versa. fetch_raw() itself is idempotent
    (existence check before any network call), so a retried task is safe.
    """
    try:
        connector = _connector()
        with session_scope() as session:
            filing = session.scalar(select(Filing).where(Filing.accession_no == accession_no))
            if filing is None:
                logger.warning("fetch_filing: unknown accession_no %s", accession_no)
                return
            company = session.get(Company, filing.cik)
            ref = _ref_for(filing, company)

            path = connector.fetch_raw(ref)
            filing.r2_bronze_key = str(path)
            filing.status = FilingStatus.FETCHED.value
            emit_event(session, "filing.fetched", accession_no, {"path": str(path)})
    except Exception as exc:
        attempts = _retry_if_transient(self, exc)
        _record_failure("filing.failed", accession_no, _failure_payload("fetch", exc, attempts))
        raise

    parse_filing.delay(accession_no)  # only after the fetch committed above


@celery_app.task(bind=True, name="filingsage.parse_filing", max_retries=MAX_RETRIES)
def parse_filing(self: Task, accession_no: str) -> None:
    """Parse one filing's bronze document into silver Parquet.

    On success: silver key + PARSED status + filing.parsed (section_count).
    On ParseQuarantineError: QUARANTINED status + filing.parse_failed
    (reason) — and no retry. Quarantine is a deterministic function of the
    bronze bytes and the section-detection rules; retrying reproduces the
    identical failure, so a retry would only waste a worker slot.

    Any OTHER error (the bronze file gone from disk, a parser bug, the
    database down) goes through the same retry/failure-event path as the
    other steps — without it, a filing that crashes here on every run would
    be re-enqueued by the reconciler forever, never counted as failing.
    """
    settings = get_settings()
    try:
        with session_scope() as session:
            filing = session.scalar(select(Filing).where(Filing.accession_no == accession_no))
            if filing is None:
                logger.warning("parse_filing: unknown accession_no %s", accession_no)
                return
            company = session.get(Company, filing.cik)
            ref = _ref_for(filing, company)
            bronze_path = Path(filing.r2_bronze_key)

            try:
                result = parse_to_silver(bronze_path, ref, settings.data_dir / "silver")
            except ParseQuarantineError as exc:
                filing.status = FilingStatus.QUARANTINED.value
                emit_event(session, "filing.parse_failed", accession_no, {"reason": str(exc)})
                return

            filing.r2_silver_key = str(result.silver_path)
            filing.status = FilingStatus.PARSED.value
            emit_event(
                session, "filing.parsed", accession_no, {"section_count": result.section_count}
            )
    except Exception as exc:
        attempts = _retry_if_transient(self, exc)
        _record_failure("filing.failed", accession_no, _failure_payload("parse", exc, attempts))
        raise

    chunk_and_embed.delay(accession_no)  # only after the parse committed above


@celery_app.task(bind=True, name="filingsage.chunk_and_embed", max_retries=MAX_RETRIES)
def chunk_and_embed(self: Task, accession_no: str) -> None:
    """Chunk a parsed filing's silver Parquet, embed it, upsert to Qdrant.

    Everything — chunk persistence, the Qdrant upsert, writing
    qdrant_point_id back onto each chunk row, and the status/event change —
    happens inside ONE session_scope, with the Qdrant upsert ordered BEFORE
    the final status->EMBEDDED write. Qdrant has no transactions of its
    own, so if it fails partway, the whole DB transaction (including any
    chunk rows persisted this run) rolls back with it — status never
    advances to EMBEDDED without the vectors actually being in Qdrant. A
    retry is safe either way: persist_chunks is idempotent (ON CONFLICT DO
    NOTHING) and Qdrant upsert is idempotent (stable point ids), so redoing
    the whole task from scratch just re-derives the same result.
    """
    try:
        with session_scope() as session:
            filing = session.scalar(select(Filing).where(Filing.accession_no == accession_no))
            if filing is None:
                logger.warning("chunk_and_embed: unknown accession_no %s", accession_no)
                return
            company = session.get(Company, filing.cik)

            gold_chunks = chunk_filing(Path(filing.r2_silver_key))
            persist_chunks(session, filing.id, gold_chunks)
            session.flush()

            rows = list(
                session.scalars(
                    select(ChunkRow)
                    .where(ChunkRow.filing_id == filing.id)
                    .order_by(ChunkRow.seq)
                )
            )

            point_ids = upsert_chunks(
                rows,
                accession_number=accession_no,
                cik=filing.cik,
                ticker=company.ticker,
                form_type=filing.form_type,
                filed_at=filing.filed_at,
            )
            for row in rows:
                row.qdrant_point_id = point_ids[row.id]

            filing.status = FilingStatus.EMBEDDED.value
            emit_event(session, "filing.embedded", accession_no, {"chunk_count": len(rows)})
    except Exception as exc:
        attempts = _retry_if_transient(self, exc)
        _record_failure("filing.failed", accession_no, _failure_payload("embed", exc, attempts))
        raise
