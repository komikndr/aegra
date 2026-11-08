"""Persist validated plans alongside immutable review snapshots."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027090000"
down_revision = "20261026160000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("swarm_reviews", sa.Column("plan", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("swarm_reviews", "plan")
