"""Bound pending scope and requirement lookups as execution history grows.

Revision ID: 0080_scope_execution_indexes
Revises: 0079_direct_directory_projection
"""

import sqlalchemy as sa
from alembic import op

revision = "0080_scope_execution_indexes"
down_revision = "0079_direct_directory_projection"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_sync_execution_scope_pending",
        "sync_execution_scope",
        ["scope_id", "requirement_id"],
        postgresql_where=sa.text("status='PENDING'"),
    )
    op.create_index(
        "ix_sync_execution_requirement", "sync_execution_scope", ["requirement_id"]
    )


def downgrade():
    op.drop_index("ix_sync_execution_requirement", table_name="sync_execution_scope")
    op.drop_index("ix_sync_execution_scope_pending", table_name="sync_execution_scope")
