"""Retain review-scoped acquisition receipts without changing original snapshots."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027100000"
down_revision = "20261027090000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("swarm_reviews", sa.Column("acquisition", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("swarm_reviews", "acquisition")
