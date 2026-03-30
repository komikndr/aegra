"""add_executive_artifacts_table

Revision ID: 20260325123000
Revises: d042a0ca1cb5
Create Date: 2026-03-25 12:30:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260325123000"
down_revision = "d042a0ca1cb5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "executive_artifacts",
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column(
            "artifact_kind",
            sa.Text(),
            server_default=sa.text("'executive_report'"),
            nullable=False,
        ),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["thread.thread_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "idx_executive_artifacts_thread_id",
        "executive_artifacts",
        ["thread_id"],
        unique=False,
    )
    op.create_index(
        "idx_executive_artifacts_user_id",
        "executive_artifacts",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "idx_executive_artifacts_created_at",
        "executive_artifacts",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_executive_artifacts_created_at", table_name="executive_artifacts"
    )
    op.drop_index("idx_executive_artifacts_user_id", table_name="executive_artifacts")
    op.drop_index("idx_executive_artifacts_thread_id", table_name="executive_artifacts")
    op.drop_table("executive_artifacts")
