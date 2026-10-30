"""Share background execution capacity across facts, links, directory and balance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0088_shared_worker_pool"
down_revision = "0087_directory_publish_budget"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("report_sync_job", sa.Column("execution_pool", sa.String(32)))
    op.create_check_constraint(
        "ck_report_execution_pool",
        "report_sync_job",
        "execution_pool IS NULL OR execution_pool IN ('background','live-report')",
    )
    op.create_index(
        "ix_report_execution_pool",
        "report_sync_job",
        ["execution_pool", "claimed_until"],
        postgresql_where=sa.text("status='RUNNING'"),
    )
    op.create_table(
        "sync_worker_pool",
        sa.Column("pool_name", sa.String(32), primary_key=True),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("prefetch_slots", sa.Integer(), nullable=False),
        sa.Column("fairness_state", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "capacity > 0 AND prefetch_slots >= 0 AND pool_name='background'",
            name="ck_sync_worker_pool_contract",
        ),
    )
    op.create_index(
        "ix_sync_schedule_leased",
        "sync_schedule",
        ["leased_until", "lease_execution_id"],
        postgresql_where=sa.text("lease_token IS NOT NULL"),
    )
    op.create_index(
        "ix_balance_active_lease", "report_balance_run", ["status", "claimed_until"]
    )


def downgrade():
    op.drop_index("ix_balance_active_lease", table_name="report_balance_run")
    op.drop_index("ix_sync_schedule_leased", table_name="sync_schedule")
    op.drop_table("sync_worker_pool")
    op.drop_index("ix_report_execution_pool", table_name="report_sync_job")
    op.drop_constraint("ck_report_execution_pool", "report_sync_job", type_="check")
    op.drop_column("report_sync_job", "execution_pool")
