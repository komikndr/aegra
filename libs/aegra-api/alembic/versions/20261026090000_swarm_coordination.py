"""Add persistent objectives, bounded reviews, task dispatch intents, and scoped journals."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261026090000"
down_revision = "20260914090000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "swarm_objectives",
        sa.Column("objective_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "request_key"),
    )
    op.create_index("ix_swarm_objectives_user", "swarm_objectives", ["user_id"])
    op.create_table(
        "swarm_reviews",
        sa.Column("review_id", sa.Text(), primary_key=True),
        sa.Column("objective_id", sa.Text(), sa.ForeignKey("swarm_objectives.objective_id"), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("request", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("objective_id", "request_key"),
        sa.UniqueConstraint("objective_id", "revision"),
    )
    op.create_index("ix_swarm_reviews_status", "swarm_reviews", ["status"])
    op.create_table(
        "swarm_tasks",
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("review_id", sa.Text(), sa.ForeignKey("swarm_reviews.review_id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("last_submitted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("result", JSONB()),
        sa.UniqueConstraint("review_id", "role"),
    )
    op.create_index("ix_swarm_tasks_status", "swarm_tasks", ["status"])
    op.create_table(
        "swarm_entries",
        sa.Column("objective_id", sa.Text(), sa.ForeignKey("swarm_objectives.objective_id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("review_id", sa.Text(), sa.ForeignKey("swarm_reviews.review_id")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    for name in ("swarm_entries", "swarm_tasks", "swarm_reviews", "swarm_objectives"):
        op.drop_table(name)
