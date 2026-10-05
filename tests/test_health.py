"""Operational surface: /stats heartbeats and needs-attention, /readyz, and
the API's background model warm-up.

/stats and /filings/needs-attention run against real Postgres
(testcontainers) with their own seed. /readyz is tested two ways: with fake
checks for the response contract (status codes, timeouts, no leaked
errors), and with the real check functions against real Postgres, Redis
(testcontainers) and in-memory Qdrant. The warm-up tests replace the model
calls, so no model is ever loaded here.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

import filingsage.db.session as db_session
from filingsage.api import main
from filingsage.config import get_settings
from filingsage.db.events import emit_event
from filingsage.db.models import Company, Event, Filing, FilingStatus
from filingsage.gold.vector_store import ensure_collection

client = TestClient(main.app)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


# --- /stats + /filings/needs-attention (real Postgres) ------------------------


@pytest.fixture(scope="module")
def engine():
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        engine = create_engine(url)
        _seed(sessionmaker(bind=engine, expire_on_commit=False))
        yield engine


@pytest.fixture
def db(engine, monkeypatch):
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(
        db_session, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False)
    )


def _failure(session, accession_no: str, error: str, attempts: int, at: datetime) -> None:
    event = emit_event(session, "filing.failed", accession_no,
                       {"step": "fetch", "error": error, "attempts": attempts})
    session.flush()
    session.execute(update(Event).where(Event.id == event.id).values(created_at=at))


def _seed(factory) -> None:
    with factory() as session, session.begin():
        session.add(Company(cik=320193, ticker="AAPL", name="Apple Inc."))
        session.flush()
        for acc, status in [
            ("acc-stuck", FilingStatus.DISCOVERED.value),      # 3 failures: needs attention
            ("acc-twice", FilingStatus.FETCHED.value),         # 2 failures: still retried
            ("acc-recovered", FilingStatus.EMBEDDED.value),    # 3 failures, then finished
            ("acc-quarantined", FilingStatus.QUARANTINED.value),
        ]:
            session.add(Filing(cik=320193, accession_no=acc, form_type="10-Q",
                               filed_at=date(2026, 7, 31), primary_document="q.htm", status=status))
        session.flush()
        for i in range(3):
            _failure(session, "acc-stuck", f"ConnectError: attempt run {i}", 5, T0 + timedelta(minutes=i))
            _failure(session, "acc-recovered", "ConnectError: x", 5, T0)
        for _ in range(2):
            _failure(session, "acc-twice", "ConnectError: y", 5, T0)
        for at in (T0, T0 + timedelta(hours=2)):
            beat = emit_event(session, "ingest.completed", "watchlist",
                              {"tickers": 10, "discovered": 200, "inserted": 0})
            session.flush()
            session.execute(update(Event).where(Event.id == beat.id).values(created_at=at))
        emit_event(session, "pipeline.reconciled", "pipeline", {"requeued": 0})


@pytest.mark.integration
def test_stats_report_heartbeats_and_problem_counts(db):
    stats = client.get("/stats").json()

    assert datetime.fromisoformat(stats["last_ingest_at"]) == T0 + timedelta(hours=2)  # the latest
    assert stats["last_reconcile_at"] is not None
    assert stats["needs_attention"] == 1  # not the 2-failure one, not the one that finished
    assert stats["quarantined"] == 1


@pytest.mark.integration
def test_needs_attention_lists_the_latest_error_and_total_attempts(db):
    [row] = client.get("/filings/needs-attention").json()

    assert (row["ticker"], row["form_type"], row["filed_at"]) == ("AAPL", "10-Q", "2026-07-31")
    assert row["accession_no"] == "acc-stuck"
    assert row["last_error"] == "ConnectError: attempt run 2"
    assert (row["failures"], row["attempts"]) == (3, 15)
    assert row["edgar_url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")


# --- /readyz: the response contract (fake checks) -----------------------------


def _ok() -> None:
    pass


def _boom() -> None:
    raise ConnectionError("secret-internal-host:6379 refused")


def _hang() -> None:
    time.sleep(5)


def test_readyz_is_200_when_every_dependency_answers(monkeypatch):
    for name in ("postgres", "redis", "qdrant"):
        monkeypatch.setitem(main.READINESS_CHECKS, name, _ok)

    resp = client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json() == {"postgres": "ok", "redis": "ok", "qdrant": "ok"}


def test_readyz_names_the_failing_dependency_without_leaking_the_error(monkeypatch):
    monkeypatch.setitem(main.READINESS_CHECKS, "postgres", _ok)
    monkeypatch.setitem(main.READINESS_CHECKS, "redis", _boom)
    monkeypatch.setitem(main.READINESS_CHECKS, "qdrant", _ok)

    resp = client.get("/readyz")

    assert resp.status_code == 503
    assert resp.json() == {"postgres": "ok", "redis": "down", "qdrant": "ok"}
    assert "secret-internal-host" not in resp.text


def test_a_hung_dependency_is_reported_down_instead_of_hanging_readyz(monkeypatch):
    monkeypatch.setattr(main, "READINESS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setitem(main.READINESS_CHECKS, "postgres", _ok)
    monkeypatch.setitem(main.READINESS_CHECKS, "redis", _ok)
    monkeypatch.setitem(main.READINESS_CHECKS, "qdrant", _hang)

    started = time.perf_counter()
    resp = client.get("/readyz")

    assert time.perf_counter() - started < 2.0
    assert resp.status_code == 503
    assert resp.json()["qdrant"] == "down"


def test_healthz_stays_liveness_only(monkeypatch):
    """A dependency being down must not fail liveness — Compose would
    restart a healthy API for it."""
    for name in ("postgres", "redis", "qdrant"):
        monkeypatch.setitem(main.READINESS_CHECKS, name, _boom)
    assert client.get("/healthz").status_code == 200


# --- /readyz: the real checks against real dependencies ---------------------


@pytest.mark.integration
def test_readyz_real_checks_pass_against_live_dependencies(db, monkeypatch):
    qdrant = QdrantClient(":memory:")
    ensure_collection(qdrant)
    monkeypatch.setattr(main, "get_client", lambda: qdrant)
    with RedisContainer() as redis_container:
        monkeypatch.setattr(main, "_readiness_redis", redis_container.get_client)
        resp = client.get("/readyz")

    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"postgres": "ok", "redis": "ok", "qdrant": "ok"}


@pytest.mark.integration
def test_readyz_real_qdrant_check_fails_without_the_collection(db, monkeypatch):
    monkeypatch.setattr(main, "get_client", lambda: QdrantClient(":memory:"))  # empty
    monkeypatch.setitem(main.READINESS_CHECKS, "redis", _ok)

    resp = client.get("/readyz")

    assert resp.status_code == 503
    assert resp.json() == {"postgres": "ok", "redis": "ok", "qdrant": "down"}


# --- model warm-up --------------------------------------------------------------


def test_the_suite_never_loads_models_at_startup():
    """tests/conftest.py's guard is in effect for every test."""
    assert get_settings().warm_models_on_startup is False


def _startup_with(monkeypatch, *, warm: bool) -> list[str]:
    """Run the app's lifespan; return the names of threads _warm_models ran in."""
    ran_in: list[str] = []
    done = threading.Event()

    def fake_warm() -> None:
        ran_in.append(threading.current_thread().name)
        done.set()

    monkeypatch.setattr(main, "get_settings",
                        lambda: SimpleNamespace(warm_models_on_startup=warm, env="test"))
    monkeypatch.setattr(main, "ensure_collection", lambda: None)
    monkeypatch.setattr(main, "_warm_models", fake_warm)
    with TestClient(main.app):
        done.wait(timeout=2)
    return ran_in


def test_startup_warms_models_in_a_background_thread(monkeypatch):
    assert _startup_with(monkeypatch, warm=True) == ["model-warmup"]


def test_startup_skips_warm_up_when_disabled(monkeypatch):
    assert _startup_with(monkeypatch, warm=False) == []


def test_warm_up_runs_one_embed_and_one_rerank_and_logs_how_long(monkeypatch, caplog):
    calls: list[str] = []
    monkeypatch.setattr(main, "embed_texts", lambda texts: calls.append("embed"))
    monkeypatch.setattr(main.rerank, "warm_up", lambda: calls.append("rerank"))

    with caplog.at_level(logging.INFO, logger=main.__name__):
        main._warm_models()

    assert calls == ["embed", "rerank"]
    assert any("models warmed in" in r.getMessage() for r in caplog.records)


def test_a_failed_warm_up_is_logged_not_raised(monkeypatch, caplog):
    def broken(texts):
        raise RuntimeError("model file missing")

    monkeypatch.setattr(main, "embed_texts", broken)
    with caplog.at_level(logging.WARNING, logger=main.__name__):
        main._warm_models()  # must not raise: the API keeps serving

    assert any("warm-up failed" in r.getMessage() for r in caplog.records)
