"""Recovery for filings whose pipeline stopped short of `embedded`.

Two tools, one module, because they share the hard part — deciding where a
filing's chain has to restart from, given what is actually on disk:

* recover_stale_filings() (`cli recover-stale`), written for one specific
  incident (Technical Decisions #27): the Fly Volume backing the worker's
  bronze/silver storage was destroyed and recreated during an earlier
  capacity/OOM incident, silently erasing every bronze .htm and silver
  .parquet file written before that point — while Postgres kept claiming
  those filings were discovered/fetched/parsed. Manual, all-at-once, throttled.

* reconcile_stuck_filings() (the `filingsage.reconcile_pipeline` beat task
  every 30 minutes, and `cli reconcile`), the routine counterpart
  (decision #37): finds filings that have made no progress for a while —
  a lost task, a worker restart, a step that gave up after its retries —
  and re-enqueues the step that moves each one forward. This also covers
  the gap recover-stale deliberately left: filings whose bronze is intact
  but which are stuck at fetched/parsed.

Bronze and silver are both fully re-derivable — bronze from EDGAR, silver
from bronze — so this is "restart the chain from wherever the disk really
is," not data recovery in the sense of recovering something unrecoverable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from filingsage.db import queries
from filingsage.db.events import emit_event
from filingsage.db.models import Filing, FilingStatus
from filingsage.db.session import session_scope
from filingsage.worker.celery_app import celery_app
from filingsage.worker.tasks import chunk_and_embed, fetch_filing, parse_filing

logger = logging.getLogger(__name__)

# Re-enqueue this many fetch_filing tasks, then pause, then the next batch —
# not all 900+ at once. EdgarClient's own rate limiter already caps outbound
# EDGAR request volume; this is about the DOWNSTREAM chain instead: every
# fetch_filing that succeeds enqueues parse_filing, which enqueues
# chunk_and_embed, and chunk_and_embed's memory profile is the thing that's
# already caused production OOM kills (README → Technical Decisions #26).
# Trickling batches in gives the worker room to actually finish one filing's
# embed before the next one queues up behind it, instead of stacking
# hundreds of memory-heavy embed tasks back to back.
RECOVERY_BATCH_SIZE = 10
RECOVERY_BATCH_DELAY_SECONDS = 30.0

# A filing is stuck when it has sat in a non-final status this long with no
# update. Generous on purpose: a step that's merely queued behind a backlog,
# or mid-retry (the longest backoff is ~4.5 min), must not look stuck.
STUCK_AFTER = timedelta(minutes=30)
# At most this many filings re-enqueued per run (every 30 min) — the same
# reasoning as the recovery batches above: a large backlog drains steadily
# instead of landing on the worker all at once.
RECONCILE_LIMIT = 25

# Filings in any of these statuses are candidates: they're not finished
# (embedded) and didn't fail for a real content reason (quarantined) — spec
# explicitly excludes both, and simply omitting them from this tuple does
# that without needing special-case branches below.
_RECOVERABLE_STATUSES = queries.IN_PROGRESS_STATUSES

# status -> the task that moves a filing forward from it.
_NEXT_STEP = {
    FilingStatus.DISCOVERED.value: fetch_filing,
    FilingStatus.FETCHED.value: parse_filing,
    FilingStatus.PARSED.value: chunk_and_embed,
}


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """What recover_stale_filings() found — and, in dry-run mode, all it did.

    `reset` and `intact` are disjoint subsets of every non-terminal filing
    examined: `reset` had no bronze file on disk (or never had one —
    'discovered' filings always land here, since they have no r2_bronze_key
    yet) and get walked from scratch; `intact` still have their bronze file
    and are left alone entirely, matching or not.
    """

    intact: list[str] = field(default_factory=list)
    reset: list[str] = field(default_factory=list)

    @property
    def total_examined(self) -> int:
        return len(self.intact) + len(self.reset)


def _exists(key: str | None) -> bool:
    return bool(key) and Path(key).exists()


def _bronze_intact(filing: Filing) -> bool:
    return _exists(filing.r2_bronze_key)


def _missing_input(filing: Filing) -> str | None:
    """Why the next step for this filing can't run from what's on disk, or
    None if it can. fetch reads nothing local; parse reads bronze; embed
    reads silver."""
    if filing.status == FilingStatus.FETCHED.value and not _exists(filing.r2_bronze_key):
        return "bronze missing on disk"
    if filing.status == FilingStatus.PARSED.value and not _exists(filing.r2_silver_key):
        return "silver missing on disk"
    return None


def _reset_to_discovered(session: Session, resets: list[tuple[str, str, str]]) -> None:
    """Send filings back to DISCOVERED — one bulk UPDATE, all-or-nothing, in
    the caller's transaction — with a filing.recovery_reset event per filing
    recording the status it had and why. `resets` is (accession_no,
    previous_status, reason). The caller re-enqueues fetch_filing after
    committing; fetch_raw() re-downloads only if the bronze file is gone."""
    session.execute(
        update(Filing)
        .where(Filing.accession_no.in_([acc for acc, _, _ in resets]))
        .values(status=FilingStatus.DISCOVERED.value)
    )
    for accession_no, previous_status, reason in resets:
        emit_event(
            session,
            "filing.recovery_reset",
            accession_no,
            {"previous_status": previous_status, "reason": reason},
        )


def recover_stale_filings(
    *,
    dry_run: bool = True,
    batch_size: int = RECOVERY_BATCH_SIZE,
    batch_delay_seconds: float = RECOVERY_BATCH_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> RecoveryPlan:
    """Classify every non-terminal filing as intact or needing a reset.

    dry_run=True (the default — the caller must opt INTO the real run):
    read-only, returns the plan without writing anything.

    dry_run=False: resets every filing in `reset` to DISCOVERED (one bulk
    UPDATE, all-or-nothing) and emits a filing.recovery_reset event per
    filing recording what its status was — then, only after that
    transaction has committed, re-enqueues fetch_filing.delay() for each
    one, in batches of `batch_size` with `batch_delay_seconds` between
    batches. `intact` filings are never touched: no status change, no
    re-enqueue (reconcile_stuck_filings() is what moves those along).
    """
    with session_scope() as session:
        filings = list(
            session.scalars(select(Filing).where(Filing.status.in_(_RECOVERABLE_STATUSES)))
        )
        intact = [f.accession_no for f in filings if _bronze_intact(f)]
        to_reset = [(f.accession_no, f.status) for f in filings if not _bronze_intact(f)]

    plan = RecoveryPlan(intact=intact, reset=[acc for acc, _ in to_reset])

    if dry_run or not to_reset:
        return plan

    accession_numbers = [acc for acc, _ in to_reset]
    with session_scope() as session:
        _reset_to_discovered(
            session, [(acc, status, "bronze missing on disk") for acc, status in to_reset]
        )

    for i in range(0, len(accession_numbers), batch_size):
        batch = accession_numbers[i : i + batch_size]
        for accession_no in batch:
            fetch_filing.delay(accession_no)
        logger.info("recover_stale_filings: enqueued batch of %d (%d/%d)",
                    len(batch), min(i + batch_size, len(accession_numbers)), len(accession_numbers))
        if i + batch_size < len(accession_numbers):
            sleep(batch_delay_seconds)

    return plan


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """One reconciler run. `requeued` includes the `reset` filings (they
    were re-enqueued too, from fetch); `needs_attention` were stuck but
    skipped for having failed too often; `remaining` were stuck but over
    this run's cap, left for the next run."""

    requeued: list[str]
    reset: list[str]
    needs_attention: int
    remaining: int

    def counts(self) -> dict[str, int]:
        return {
            "requeued": len(self.requeued),
            "reset": len(self.reset),
            "needs_attention": self.needs_attention,
            "remaining": self.remaining,
        }


def reconcile_stuck_filings(
    *, now: datetime | None = None, limit: int = RECONCILE_LIMIT
) -> ReconcileResult:
    """Re-enqueue the next step for every filing stuck in a non-final status.

    Stuck = discovered/fetched/parsed with no update for STUCK_AFTER. Each
    gets the task that moves it on (discovered -> fetch, fetched -> parse,
    parsed -> embed) — unless the file that task reads is missing on disk,
    in which case it's reset to discovered and fetched again (the same
    reset recover_stale_filings uses). Filings with NEEDS_ATTENTION_FAILURES
    or more filing.failed events are skipped: they've had their retries,
    and repeating them on a timer forever would hide them rather than fix
    them. Oldest first, at most `limit` per run.

    One transaction records everything — status resets, a fresh updated_at
    on each re-queued filing (so a step still waiting in a long queue isn't
    re-queued again next run), a filing.requeued event per filing and one
    pipeline.reconciled summary, written even when nothing was stuck so the
    dashboard can show the reconciler is alive. Tasks are enqueued only
    after it commits.
    """
    cutoff = (now or datetime.now(UTC)) - STUCK_AFTER
    failed = queries.failed_runs()
    failures = func.coalesce(failed.c.failures, 0)
    stuck = (
        select(Filing)
        .outerjoin(failed, failed.c.accession_no == Filing.accession_no)
        .where(Filing.status.in_(_RECOVERABLE_STATUSES), Filing.updated_at < cutoff)
    )

    with session_scope() as session:
        retryable = stuck.where(failures < queries.NEEDS_ATTENTION_FAILURES)
        candidates = list(
            session.scalars(retryable.order_by(Filing.updated_at, Filing.id).limit(limit))
        )
        total_retryable = session.scalar(select(func.count()).select_from(retryable.subquery()))
        needs_attention = session.scalar(
            select(func.count()).select_from(
                stuck.where(failures >= queries.NEEDS_ATTENTION_FAILURES).subquery()
            )
        )

        to_enqueue: list[tuple[str, str]] = []  # (accession_no, status it moves on from)
        resets: list[tuple[str, str, str]] = []
        for filing in candidates:
            reason = _missing_input(filing)
            if reason:
                resets.append((filing.accession_no, filing.status, reason))
                to_enqueue.append((filing.accession_no, FilingStatus.DISCOVERED.value))
            else:
                to_enqueue.append((filing.accession_no, filing.status))
            emit_event(session, "filing.requeued", filing.accession_no, {"from_status": filing.status})
        if resets:
            _reset_to_discovered(session, resets)
        if candidates:
            # Explicit, because only the reset filings go through an UPDATE
            # that would bump it on its own.
            session.execute(
                update(Filing)
                .where(Filing.id.in_([f.id for f in candidates]))
                .values(updated_at=func.now())
            )

        result = ReconcileResult(
            requeued=[acc for acc, _ in to_enqueue],
            reset=[acc for acc, _, _ in resets],
            needs_attention=needs_attention or 0,
            remaining=(total_retryable or 0) - len(candidates),
        )
        emit_event(session, "pipeline.reconciled", "pipeline", result.counts())

    for accession_no, status in to_enqueue:
        _NEXT_STEP[status].delay(accession_no)
    if result.requeued or result.needs_attention:
        logger.info("reconcile_stuck_filings: %s", result.counts())
    return result


@celery_app.task(name="filingsage.reconcile_pipeline")
def reconcile_pipeline() -> dict:
    """Beat's entrypoint for the reconciler — every 30 minutes
    (celery_app.beat_schedule)."""
    return reconcile_stuck_filings().counts()
