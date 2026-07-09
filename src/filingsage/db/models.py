"""Core schema, spec §4: companies, filings, events (auth tables land Week 3).

Schema-as-code: this metadata is the single source of truth; Alembic
autogenerates migrations by diffing against it.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Integer,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on Postgres (binary, indexable); plain JSON elsewhere (e.g. a quick
# sqlite smoke test). One model, per-dialect storage.
PortableJSON = JSON().with_variant(JSONB(), "postgresql")

# BIGSERIAL on Postgres; plain INTEGER on sqlite, whose autoincrement only
# works on INTEGER PRIMARY KEY. Same model, correct DDL per dialect.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class FilingStatus(str, enum.Enum):
    """Stored as strings, validated in code — not a Postgres ENUM.

    PG enums make every new status a DDL migration; a String column with
    code-level validation keeps the pipeline's vocabulary cheap to evolve.
    """

    DISCOVERED = "discovered"
    FETCHED = "fetched"
    PARSED = "parsed"
    QUARANTINED = "quarantined"


class Company(Base):
    __tablename__ = "companies"

    cik: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ticker: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Filing(Base):
    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik"), index=True)
    accession_no: Mapped[str] = mapped_column(String(25), unique=True, index=True)
    form_type: Mapped[str] = mapped_column(String(10), index=True)
    filed_at: Mapped[date] = mapped_column(Date, index=True)
    primary_document: Mapped[str] = mapped_column(Text)
    r2_bronze_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    r2_silver_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=FilingStatus.DISCOVERED.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Event(Base):
    """Append-only audit trail; every pipeline step writes one row (spec §3)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict] = mapped_column(PortableJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )