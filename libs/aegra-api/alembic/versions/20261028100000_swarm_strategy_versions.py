"""Persist immutable strategic-search branch versions."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261028100000"
down_revision = "20261027100000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("swarm_strategy_versions",
                    sa.Column("review_id", sa.Text(), sa.ForeignKey("swarm_reviews.review_id"), primary_key=True),
                    sa.Column("branch_id", sa.Text(), primary_key=True),
                    sa.Column("version", sa.Integer(), primary_key=True),
                    sa.Column("content", postgresql.JSONB(), nullable=False),
                    sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")))


def downgrade() -> None:
    op.drop_table("swarm_strategy_versions")
