"""Reference-count query waiters and index settled sync dispatches.

Revision ID: 0074_query_waiters
Revises: 0073_projection_coverage
"""

from alembic import op

revision = "0074_query_waiters"
down_revision = "0073_projection_coverage"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE query_snapshot_build ADD COLUMN backend_pid INTEGER")
    op.execute(
        'CREATE TABLE query_snapshot_waiter (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tbc_id VARCHAR(128) NOT NULL, \n\tactor_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tkey VARCHAR(64) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id), \n\tFOREIGN KEY(actor_id) REFERENCES "user" (id)\n)'
    )
    op.execute(
        "CREATE INDEX ix_query_snapshot_waiter_expiry ON query_snapshot_waiter (expires_at)"
    )
    op.execute(
        "CREATE INDEX ix_query_snapshot_waiter_key_expiry ON query_snapshot_waiter (key, expires_at)"
    )
    op.execute(
        "CREATE INDEX ix_dispatch_sync_compaction ON pending_dispatch (published_at, id) WHERE published_at IS NOT NULL AND compacted_at IS NULL AND task_name IN ('reporting.job_step','reporting.backfill_step','ads.sync_step','ads.publish_step','reporting.balance_step')"
    )


def downgrade():
    op.drop_index("ix_dispatch_sync_compaction", table_name="pending_dispatch")
    op.drop_table("query_snapshot_waiter")
    op.drop_column("query_snapshot_build", "backend_pid")
