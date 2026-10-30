"""Distinguish offline supersession from successful broker publication.

Revision ID: 0085_dispatch_supersession
Revises: 0084_balance_retry_budget
"""

import sqlalchemy as sa
from alembic import op

revision = "0085_dispatch_supersession"
down_revision = "0084_balance_retry_budget"
branch_labels = None
depends_on = None


def _indexes(supersession: bool):
    condition = "published_at IS NULL"
    if supersession:
        condition += " AND superseded_at IS NULL"
    for name, columns, suffix in (
        ("ix_dispatch_pending", ["available_at", "id"], ""),
        ("ix_dispatch_tenant_pending", ["tenant_id", "available_at", "id"], ""),
        (
            "ix_dispatch_expansion_pending",
            ["tenant_id", "available_at", "id"],
            " AND task_name = 'builds.expand_submission'",
        ),
    ):
        op.drop_index(name, table_name="pending_dispatch")
        op.create_index(
            name,
            "pending_dispatch",
            columns,
            postgresql_where=sa.text(condition + suffix),
        )


def upgrade():
    op.add_column(
        "pending_dispatch", sa.Column("superseded_at", sa.DateTime(timezone=True))
    )
    _indexes(True)
    op.create_index(
        "ix_dispatch_superseded_compaction",
        "pending_dispatch",
        ["superseded_at", "id"],
        postgresql_where=sa.text("superseded_at IS NOT NULL AND compacted_at IS NULL"),
    )


def downgrade():
    op.drop_index(
        "ix_dispatch_superseded_compaction",
        table_name="pending_dispatch",
        if_exists=True,
    )
    _indexes(False)
    op.drop_column("pending_dispatch", "superseded_at")
