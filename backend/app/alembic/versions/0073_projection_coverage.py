"""Freeze derived coverage and identify the execution holding a scheduling lease.

Revision ID: 0073_projection_coverage
Revises: 0072_snapshot_leases
"""

import sqlalchemy as sa
from alembic import op

revision = "0073_projection_coverage"
down_revision = "0072_snapshot_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "read_bucket_state",
        sa.Column("derived_observed_at", sa.DateTime(timezone=True)),
    )
    op.add_column("read_bucket_state", sa.Column("derived_status", sa.String(16)))
    op.execute(
        "UPDATE read_bucket_state SET source_revision=source_revision+1 WHERE derived_revision>0 AND kind IN ('ad_day','material_day')"
    )
    op.add_column("sync_schedule", sa.Column("lease_execution_id", sa.Uuid()))
    op.add_column(
        "sync_schedule", sa.Column("lease_execution_generation", sa.Integer())
    )
    # DEFAULT兜底历史缺月；维护每轮只找最早月份，不能扫描整个缺月事实集。
    op.create_index("ix_ad_report_default_date", "ad_report_day_default", ["stat_date"])
    op.create_index(
        "ix_material_report_default_date", "material_report_day_default", ["stat_date"]
    )


def downgrade():
    op.drop_index(
        "ix_material_report_default_date", table_name="material_report_day_default"
    )
    op.drop_index("ix_ad_report_default_date", table_name="ad_report_day_default")
    op.drop_column("sync_schedule", "lease_execution_generation")
    op.drop_column("sync_schedule", "lease_execution_id")
    op.drop_column("read_bucket_state", "derived_status")
    op.drop_column("read_bucket_state", "derived_observed_at")
