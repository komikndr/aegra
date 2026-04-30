"""add_user_memory_tables

Revision ID: 20260501093000
Revises: 20260325123000
Create Date: 2026-05-01 09:30:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260501093000"
down_revision = "20260325123000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_memories",
        sa.Column(
            "memory_id",
            sa.Text(),
            server_default=sa.text("public.uuid_generate_v4()::text"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'fact'"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_thread_id", sa.Text(), nullable=True),
        sa.Column("source_run_id", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index("idx_user_memories_user", "user_memories", ["user_id"])
    op.create_index(
        "idx_user_memories_user_content",
        "user_memories",
        ["user_id", "content"],
        unique=True,
    )

    op.create_table(
        "user_memory_snapshots",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("memory_hash", sa.Text(), server_default=sa.text("''"), nullable=False),
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
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "idx_user_memory_snapshots_updated_at",
        "user_memory_snapshots",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_user_memory_snapshots_updated_at", table_name="user_memory_snapshots")
    op.drop_table("user_memory_snapshots")
    op.drop_index("idx_user_memories_user_content", table_name="user_memories")
    op.drop_index("idx_user_memories_user", table_name="user_memories")
    op.drop_table("user_memories")
