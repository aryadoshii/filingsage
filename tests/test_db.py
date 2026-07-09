"""Integration tests: real Postgres via testcontainers, real alembic migrations.

Requires a running Docker daemon.
Fast unit loop without these: pytest -m "not integration"
"""

from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from filingsage.db.events import emit_event
from filingsage.db.models import Company, Event, Filing, FilingStatus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")  # test the real migration path, not create_all
        yield create_engine(url)


def test_migration_applies_and_pipeline_rows_roundtrip(engine):
    with Session(engine) as s:
        s.add(Company(cik=320193, ticker="AAPL", name="Apple Inc."))
        s.add(
            Filing(cik=320193, accession_no="0000320193-26-000013", form_type="10-Q",
                   filed_at=date(2026, 5, 1), primary_document="aapl.htm")
        )
        emit_event(s, "filing.discovered", "0000320193-26-000013", {"ticker": "AAPL"})
        s.commit()

    with Session(engine) as s:
        filing = s.scalars(select(Filing)).one()
        event = s.scalars(select(Event)).one()
        assert filing.status == FilingStatus.DISCOVERED.value
        assert event.type == "filing.discovered"
        assert event.payload_json == {"ticker": "AAPL"}  # JSONB round-trip
        assert event.created_at is not None              # server default fired


def test_accession_number_is_unique(engine):
    with Session(engine) as s:
        s.add(Company(cik=789019, ticker="MSFT", name="Microsoft"))
        s.add(Filing(cik=789019, accession_no="dup-1", form_type="8-K",
                     filed_at=date(2026, 6, 1), primary_document="a.htm"))
        s.commit()
    with Session(engine) as s:
        s.add(Filing(cik=789019, accession_no="dup-1", form_type="8-K",
                     filed_at=date(2026, 6, 2), primary_document="b.htm"))
        with pytest.raises(IntegrityError):
            s.commit()