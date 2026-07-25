"""Recovery for filings whose Postgres status outran what's actually on disk.

Written for one specific incident (README → Technical Decisions #27): the
Fly Volume backing the worker's bronze/silver storage was destroyed and
recreated during an earlier capacity/OOM incident, silently erasing every
bronze .htm and silver .parquet file written before that point — while
Postgres kept claiming those filings were discovered/fetched/parsed. But
the tool is generic to the failure shape, not the specific incident: if a
future volume issue does the same thing, this is the documented, repeatable
way to recover, not a one-off script someone has to reconstruct from memory.

Bronze and silver are both fully re-derivable — bronze from EDGAR, silver
from bronze — so this is "restart the chain from wherever the disk really
is," not data recovery in the sense of recovering something unrecoverable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update

from filingsage.db.events import emit_event
from filingsage.db.models import Filing, FilingStatus
from filingsage.db.session import session_scope
from filingsage.worker.tasks import fetch_filing

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

# Filings in any of these statuses are candidates: they're not finished
# (embedded) and didn't fail for a real content reason (quarantined) — spec
# explicitly excludes both, and simply omitting them from this tuple does
# that without needing special-case branches below.
_RECOVERABLE_STATUSES = (
    FilingStatus.DISCOVERED.value,
    FilingStatus.FETCHED.value,
    FilingStatus.PARSED.value,
)


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


def _bronze_intact(filing: Filing) -> bool:
    return bool(filing.r2_bronze_key) and Path(filing.r2_bronze_key).exists()


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
    re-enqueue.
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
        session.execute(
            update(Filing)
            .where(Filing.accession_no.in_(accession_numbers))
            .values(status=FilingStatus.DISCOVERED.value)
        )
        for accession_no, previous_status in to_reset:
            emit_event(
                session,
                "filing.recovery_reset",
                accession_no,
                {"previous_status": previous_status, "reason": "bronze missing on disk"},
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
