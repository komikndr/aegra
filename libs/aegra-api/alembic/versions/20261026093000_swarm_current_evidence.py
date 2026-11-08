"""Retain a bounded current-evidence projection without rewriting historical entries."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261026093000"
down_revision = "20261026090000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("swarm_objectives", sa.Column("current_evidence", JSONB(), nullable=True))
    op.execute("UPDATE swarm_objectives SET current_evidence = COALESCE(definition->'evidence', '[]'::jsonb)")
    op.alter_column("swarm_objectives", "current_evidence", nullable=False)


def downgrade() -> None:
    op.drop_column("swarm_objectives", "current_evidence")
