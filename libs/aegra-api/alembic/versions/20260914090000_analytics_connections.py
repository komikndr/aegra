"""Add user-owned Analytics connections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260914090000"
down_revision = "20260718160000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_connections",
        sa.Column("connection_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("dag_ids", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_analytics_connections_user_id", "analytics_connections", ["user_id"])


def downgrade() -> None:
    op.drop_table("analytics_connections")
