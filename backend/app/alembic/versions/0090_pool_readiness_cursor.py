"""Bound ready-class discovery without starving scopes behind blocked endpoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0090_pool_readiness_cursor"
down_revision = "0089_sync_cost_abandonment"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sync_worker_pool",
        sa.Column(
            "readiness_cursor",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("sync_worker_pool", "readiness_cursor", server_default=None)


def downgrade():
    op.drop_column("sync_worker_pool", "readiness_cursor")
