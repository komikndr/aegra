"""Idempotent paper-loop start requests."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261026113000"
down_revision = "20261026110000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_commands",
        sa.Column("account_id", sa.Text(), sa.ForeignKey("paper_accounts.account_id"), primary_key=True),
        sa.Column("request_key", sa.Text(), primary_key=True),
        sa.Column("request", JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("paper_commands")
