"""Recover durable archive markers after source deletion without permanent DB manifests."""

import sqlalchemy as sa
from alembic import op

revision = "0093_sync_archive_finalization"
down_revision = "0092_run_archive_timestamps"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sync_archive_finalization",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("execution_id", sa.Uuid(), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.CheckConstraint(
            "kind IN ('report','directory','balance') AND byte_length>0 AND digest ~ '^[a-f0-9]{64}$'",
            name="ck_sync_archive_finalization_contract",
        ),
    )
    op.create_index(
        "ix_sync_archive_finalize_due",
        "sync_archive_finalization",
        ["created_at", "tenant_id", "kind", "execution_id"],
    )


def downgrade():
    op.drop_index(
        "ix_sync_archive_finalize_due", table_name="sync_archive_finalization"
    )
    op.drop_table("sync_archive_finalization")
