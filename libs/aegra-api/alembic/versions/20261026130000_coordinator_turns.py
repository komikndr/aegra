"""Add durable objective Coordinator conversations."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261026130000"
down_revision = "20261026113000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coordinator_turns",
        sa.Column("turn_id", sa.Text(), primary_key=True),
        sa.Column("objective_id", sa.Text(), sa.ForeignKey("swarm_objectives.objective_id"), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("objective_id", "request_key"),
    )
    op.create_index("ix_coordinator_turns_pending", "coordinator_turns", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("coordinator_turns")
