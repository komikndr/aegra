"""Add encrypted message gateway identity and conversation state.

Revision ID: 20260718150000
Revises: 20260718090000
Create Date: 2026-07-18 15:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "20260718150000"
down_revision = "20260718090000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_gateway_link_attempt",
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("principal_ref", sa.String(length=64), nullable=False),
        sa.Column("device_code_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("user_code", sa.String(length=64), nullable=False),
        sa.Column("verification_uri", sa.Text(), nullable=False),
        sa.Column("verification_uri_complete", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("platform", "principal_ref", name="uq_gateway_attempt_principal"),
    )
    op.create_table(
        "message_gateway_identity_link",
        sa.Column("link_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("principal_ref", sa.String(length=64), nullable=False),
        sa.Column("keycloak_subject", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("selected_assistant_id", sa.Text(), nullable=True),
        sa.Column("selected_graph_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("link_id"),
        sa.UniqueConstraint("platform", "principal_ref", name="uq_gateway_link_principal"),
    )
    op.create_index(
        "idx_gateway_link_subject",
        "message_gateway_identity_link",
        ["keycloak_subject"],
        unique=False,
    )
    op.create_table(
        "message_gateway_conversation",
        sa.Column("mapping_id", sa.Text(), nullable=False),
        sa.Column("link_id", sa.Text(), nullable=False),
        sa.Column("conversation_ref", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.Text(), nullable=False),
        sa.Column("graph_id", sa.Text(), nullable=False),
        sa.Column("aegra_thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["link_id"],
            ["message_gateway_identity_link.link_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mapping_id"),
        sa.UniqueConstraint(
            "link_id",
            "conversation_ref",
            "assistant_id",
            name="uq_gateway_conversation_assistant",
        ),
    )
    op.create_index(
        "idx_gateway_conversation_thread",
        "message_gateway_conversation",
        ["aegra_thread_id"],
        unique=True,
    )
    op.create_table(
        "message_gateway_inbound_message",
        sa.Column("inbound_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("message_ref", sa.String(length=64), nullable=False),
        sa.Column("link_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["link_id"],
            ["message_gateway_identity_link.link_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("inbound_id"),
        sa.UniqueConstraint("platform", "message_ref", name="uq_gateway_inbound_message"),
    )


def downgrade() -> None:
    op.drop_table("message_gateway_inbound_message")
    op.drop_index("idx_gateway_conversation_thread", table_name="message_gateway_conversation")
    op.drop_table("message_gateway_conversation")
    op.drop_index("idx_gateway_link_subject", table_name="message_gateway_identity_link")
    op.drop_table("message_gateway_identity_link")
    op.drop_table("message_gateway_link_attempt")
