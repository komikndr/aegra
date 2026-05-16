"""add_pipeline_workflow_tables

Revision ID: 20260516090000
Revises: 20260501093000
Create Date: 2026-05-16 09:00:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260516090000"
down_revision = "20260501093000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_workflows",
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "graph",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("workflow_id"),
    )
    op.create_index("idx_pipeline_workflows_user", "pipeline_workflows", ["user_id"])
    op.create_index("idx_pipeline_workflows_user_name", "pipeline_workflows", ["user_id", "name"], unique=True)

    op.create_table(
        "pipeline_api_keys",
        sa.Column("key_id", sa.Text(), server_default=sa.text("public.uuid_generate_v4()::text"), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workflow_id"], ["pipeline_workflows.workflow_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("key_id"),
    )
    op.create_index("idx_pipeline_api_keys_workflow", "pipeline_api_keys", ["workflow_id"])
    op.create_index("idx_pipeline_api_keys_user", "pipeline_api_keys", ["user_id"])
    op.create_index("idx_pipeline_api_keys_hash", "pipeline_api_keys", ["key_hash"], unique=True)

    op.create_table(
        "pipeline_runs",
        sa.Column("run_id", sa.Text(), server_default=sa.text("public.uuid_generate_v4()::text"), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'completed'"), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["pipeline_workflows.workflow_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("idx_pipeline_runs_workflow", "pipeline_runs", ["workflow_id"])
    op.create_index("idx_pipeline_runs_user", "pipeline_runs", ["user_id"])
    op.create_index("idx_pipeline_runs_created_at", "pipeline_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_pipeline_runs_created_at", table_name="pipeline_runs")
    op.drop_index("idx_pipeline_runs_user", table_name="pipeline_runs")
    op.drop_index("idx_pipeline_runs_workflow", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
    op.drop_index("idx_pipeline_api_keys_hash", table_name="pipeline_api_keys")
    op.drop_index("idx_pipeline_api_keys_user", table_name="pipeline_api_keys")
    op.drop_index("idx_pipeline_api_keys_workflow", table_name="pipeline_api_keys")
    op.drop_table("pipeline_api_keys")
    op.drop_index("idx_pipeline_workflows_user_name", table_name="pipeline_workflows")
    op.drop_index("idx_pipeline_workflows_user", table_name="pipeline_workflows")
    op.drop_table("pipeline_workflows")
