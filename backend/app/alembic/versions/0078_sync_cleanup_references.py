"""Bound owner cleanup reference checks and pure wake compaction.

Revision ID: 0078_sync_cleanup_references
Revises: 0077_dirty_bucket_claim
"""

import sqlalchemy as sa
from alembic import op

revision = "0078_sync_cleanup_references"
down_revision = "0077_dirty_bucket_claim"
branch_labels = None
depends_on = None


def upgrade():
    for key in ("job_id", "run_id"):
        op.create_index(
            f"ix_dispatch_uncompacted_{key}",
            "pending_dispatch",
            ["tenant_id", sa.text(f"(payload ->> '{key}')")],
            postgresql_where=sa.text(
                f"compacted_at IS NULL AND payload ->> '{key}' IS NOT NULL"
            ),
        )
    op.create_index(
        "ix_dispatch_pure_wake_compaction",
        "pending_dispatch",
        ["published_at", "id"],
        postgresql_where=sa.text(
            "published_at IS NOT NULL AND compacted_at IS NULL AND task_name='reporting.plan_due'"
        ),
    )


def downgrade():
    op.drop_index("ix_dispatch_pure_wake_compaction", table_name="pending_dispatch")
    for key in ("job_id", "run_id"):
        op.drop_index(f"ix_dispatch_uncompacted_{key}", table_name="pending_dispatch")
