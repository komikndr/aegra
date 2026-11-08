"""Paper-only accounts, market snapshots, recoverable cycles, and fill ledger."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261026110000"
down_revision = "20261026093000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_accounts",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("policy", JSONB(), nullable=False),
        sa.Column("cash", sa.Numeric(24, 4), nullable=False),
        sa.Column("positions", JSONB(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("refresh_esi", sa.Boolean(), nullable=False),
        sa.Column("next_due", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("user_id", "request_key"),
    )
    op.create_table(
        "paper_quotes",
        sa.Column("quote_id", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Text(), sa.ForeignKey("paper_accounts.account_id"), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_bid", sa.BigInteger(), nullable=False),
        sa.Column("used_ask", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "paper_cycles",
        sa.Column("cycle_id", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Text(), sa.ForeignKey("paper_accounts.account_id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("result", JSONB()),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "number"),
    )
    op.create_table(
        "paper_ledger",
        sa.Column("cycle_id", sa.Text(), sa.ForeignKey("paper_cycles.cycle_id"), primary_key=True),
        sa.Column("account_id", sa.Text(), sa.ForeignKey("paper_accounts.account_id"), nullable=False),
        sa.Column("entry", JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for table in ("paper_accounts", "paper_quotes", "paper_cycles", "paper_ledger"):
        column = "user_id" if table == "paper_accounts" else "account_id"
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table in ("paper_ledger", "paper_cycles", "paper_quotes", "paper_accounts"):
        op.drop_table(table)
