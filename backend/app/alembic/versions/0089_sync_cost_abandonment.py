"""Seal unknown costs after execution invalidation without refunding them."""

import sqlalchemy as sa
from alembic import op

revision = "0089_sync_cost_abandonment"
down_revision = "0088_shared_worker_pool"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sync_cost_ledger",
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_sync_cost_abandonable",
        "sync_cost_ledger",
        ["lease_until", "id"],
        postgresql_where=sa.text("closed IS FALSE AND abandoned_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_sync_cost_abandonable", table_name="sync_cost_ledger")
    op.drop_column("sync_cost_ledger", "abandoned_at")
