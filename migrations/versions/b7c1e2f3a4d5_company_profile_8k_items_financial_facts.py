"""company profile fields, 8-K item codes, financial_facts

Revision ID: b7c1e2f3a4d5
Revises: 6ca5ca7f71c4
Create Date: 2026-10-04 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c1e2f3a4d5"
down_revision: Union[str, Sequence[str], None] = "6ca5ca7f71c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("fiscal_year_end", sa.String(length=4), nullable=True))
    op.add_column("companies", sa.Column("exchange", sa.String(length=16), nullable=True))
    op.add_column(
        "companies",
        sa.Column("financials_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("filings", sa.Column("items", sa.Text(), nullable=True))
    op.create_table(
        "financial_facts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.BigInteger(), nullable=False),
        sa.Column("metric", sa.String(length=48), nullable=False),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("start", sa.Date(), nullable=True),
        sa.Column("end", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("derived", sa.Boolean(), nullable=False),
        sa.Column("concept", sa.String(length=128), nullable=False),
        sa.Column("accession_no", sa.String(length=25), nullable=True),
        sa.Column("filed", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["cik"], ["companies.cik"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cik", "metric", "period", "end", name="uq_financial_facts_period"),
    )
    op.create_index(op.f("ix_financial_facts_cik"), "financial_facts", ["cik"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_financial_facts_cik"), table_name="financial_facts")
    op.drop_table("financial_facts")
    op.drop_column("filings", "items")
    op.drop_column("companies", "financials_updated_at")
    op.drop_column("companies", "exchange")
    op.drop_column("companies", "fiscal_year_end")
