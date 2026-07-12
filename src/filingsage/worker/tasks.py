"""Celery tasks: the discover -> fetch -> parse ingestion pipeline (spec §3).

Each task takes only an accession_no string (task-argument hygiene): task
payloads stay small and JSON-serializable, and the Filing row — not a stale
snapshot captured at enqueue time — is the source of truth when a task
actually runs.

Chaining is explicit (`.delay()` from inside the previous task) rather than
a Celery `chain()`/`chord()` primitive: each step's DB write must commit
before the next step is enqueued, and each step independently decides
whether to continue (e.g. parse_filing does not re-enqueue on quarantine).
"""

from __future__ import annotations

from pathlib import Path

from celery.utils.log import get_task_logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from filingsage.config import get_settings
from filingsage.connectors import EdgarClient, EdgarConnector, FilingRef
from filingsage.db.events import emit_event
from filingsage.db.models import Company, Filing, FilingStatus
from filingsage.db.session import session_scope
from filingsage.parsing.silver import ParseQuarantineError, parse_to_silver
from filingsage.worker.celery_app import celery_app

logger = get_task_logger(__name__)


def _connector() -> EdgarConnector:
    """Construction seam: tests monkeypatch this instead of building a real client."""
    settings = get_settings()
    client = EdgarClient(contact_email=settings.sec_contact_email)
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


@celery_app.task(name="filingsage.ping")
def ping() -> str:
    """Round-trip smoke test: API container -> Redis -> worker -> Redis -> caller."""
    return "pong"


@celery_app.task(name="filingsage.ingest_watchlist")
def ingest_watchlist(tickers: list[str], limit: int | None = None) -> dict:
    """Discover filings for `tickers`, insert genuinely-new ones, enqueue fetches.

    The dedupe gate: `INSERT ... ON CONFLICT (accession_no) DO NOTHING
    RETURNING accession_no` inserts nothing and returns nothing for an
    accession we've already seen. Only rows that come back from RETURNING
    are new — those, and only those, get a filing.discovered event and a
    fetch_filing enqueue. This is what makes the periodic cron re-runnable
    for free: run it again with the same watchlist and it's a no-op past
    the first pass.
    """
    connector = _connector()
    refs = connector.discover(tickers)

    by_ticker: dict[str, list[FilingRef]] = {}
    for ref in refs:
        by_ticker.setdefault(ref.ticker, []).append(ref)

    newly_inserted: list[str] = []
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
                        status=FilingStatus.DISCOVERED.value,
                    )
                    .on_conflict_do_nothing(index_elements=["accession_no"])
                    .returning(Filing.accession_no)
                )
                if result.first() is None:
                    continue  # already known — dedupe gate, skip silently
                newly_inserted.append(ref.accession_number)
                emit_event(
                    session,
                    "filing.discovered",
                    ref.accession_number,
                    {"ticker": ref.ticker, "form_type": ref.form_type},
                )

    # Enqueue only after the transaction committed — never fetch a filing
    # whose "discovered" row might not actually be in the database.
    for accession_no in newly_inserted:
        fetch_filing.delay(accession_no)

    return {"discovered": len(refs), "inserted": len(newly_inserted)}


@celery_app.task(name="filingsage.fetch_filing")
def fetch_filing(accession_no: str) -> None:
    """Fetch one filing's primary document into bronze; enqueue parse_filing.

    Status change, bronze key, and the filing.fetched event all commit in one
    session_scope — a crash mid-task can never leave the DB claiming FETCHED
    without a bronze file, or vice versa. fetch_raw() itself is idempotent
    (existence check before any network call), so a retried task is safe.
    """
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

    parse_filing.delay(accession_no)  # only after the fetch committed above


@celery_app.task(name="filingsage.parse_filing")
def parse_filing(accession_no: str) -> None:
    """Parse one filing's bronze document into silver Parquet.

    On success: silver key + PARSED status + filing.parsed (section_count).
    On ParseQuarantineError: QUARANTINED status + filing.parse_failed
    (reason) — and no retry. Quarantine is a deterministic function of the
    bronze bytes and the section-detection rules; retrying reproduces the
    identical failure, so a retry would only waste a worker slot.
    """
    settings = get_settings()
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
