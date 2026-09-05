"""Add atomic processing leases and retention support to gateway messages.

Revision ID: 20260718160000
Revises: 20260718150000
Create Date: 2026-07-18 16:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260718160000"
down_revision = "20260718150000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_gateway_inbound_message",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "message_gateway_inbound_message",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE message_gateway_inbound_message "
        "SET updated_at = COALESCE(completed_at, created_at) "
        "WHERE updated_at IS NULL"
    )
    op.alter_column("message_gateway_inbound_message", "updated_at", nullable=False)
    op.create_check_constraint(
        "ck_gateway_inbound_status",
        "message_gateway_inbound_message",
        "status IN ('processing', 'completed', 'failed')",
    )
    op.create_index(
        "idx_gateway_inbound_retention",
        "message_gateway_inbound_message",
        ["status", "completed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_gateway_inbound_retention", table_name="message_gateway_inbound_message")
    op.drop_constraint(
        "ck_gateway_inbound_status",
        "message_gateway_inbound_message",
        type_="check",
    )
    op.drop_column("message_gateway_inbound_message", "lease_expires_at")
    op.drop_column("message_gateway_inbound_message", "updated_at")
