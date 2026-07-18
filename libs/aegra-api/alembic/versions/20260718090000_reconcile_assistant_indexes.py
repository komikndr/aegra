"""Reconcile assistant metadata and fixed-width config indexes.

Revision ID: 20260718090000
Revises: 20260516090000
Create Date: 2026-07-18 09:00:00
"""

from alembic import op

revision = "20260718090000"
down_revision = "20260516090000"
branch_labels = None
depends_on = None

_CONFIG_INDEX = "idx_assistant_user_graph_config"
_CONFIG_INDEX_NEW = "idx_assistant_user_graph_config_md5"
_METADATA_INDEX = "idx_assistant_metadata_gin"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_METADATA_INDEX} "
            "ON assistant USING gin (metadata jsonb_path_ops)"
        )
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_CONFIG_INDEX_NEW} "
            "ON assistant (user_id, graph_id, md5(config::text))"
        )

    op.execute(f"DROP INDEX IF EXISTS {_CONFIG_INDEX}")
    op.execute(f"ALTER INDEX {_CONFIG_INDEX_NEW} RENAME TO {_CONFIG_INDEX}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_CONFIG_INDEX_NEW} "
            "ON assistant (user_id, graph_id, config)"
        )

    op.execute(f"DROP INDEX IF EXISTS {_CONFIG_INDEX}")
    op.execute(f"ALTER INDEX {_CONFIG_INDEX_NEW} RENAME TO {_CONFIG_INDEX}")

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_METADATA_INDEX}")
