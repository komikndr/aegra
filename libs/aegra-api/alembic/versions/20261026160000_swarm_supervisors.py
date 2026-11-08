"""Add bounded persistent objective supervisors and command deduplication."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261026160000"
down_revision = "20261026130000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "swarm_supervisors",
        sa.Column("objective_id", sa.Text(), sa.ForeignKey("swarm_objectives.objective_id"), primary_key=True),
        sa.Column("generation", sa.Text(), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("next_wakeup", sa.DateTime(timezone=True)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviews_started", sa.Integer(), nullable=False),
        sa.Column("model_calls_reserved", sa.Integer(), nullable=False),
        sa.Column("specialist_tasks_reserved", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.Text(), sa.ForeignKey("swarm_reviews.review_id")),
        sa.Column("owns_review", sa.Boolean(), nullable=False),
        sa.Column("evidence_fingerprint", sa.Text(), nullable=False),
        sa.Column("wake_pending", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_swarm_supervisors_enabled", "swarm_supervisors", ["enabled", "checked_at"])
    op.create_table(
        "swarm_supervisor_commands",
        sa.Column("objective_id", sa.Text(), sa.ForeignKey("swarm_objectives.objective_id"), primary_key=True),
        sa.Column("request_key", sa.Text(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("swarm_supervisor_commands")
    op.drop_table("swarm_supervisors")
