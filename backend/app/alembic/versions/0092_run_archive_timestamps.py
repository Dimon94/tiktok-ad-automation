"""Retain terminal summaries from a known completion or migration observation time."""

import sqlalchemy as sa
from alembic import op

revision = "0092_run_archive_timestamps"
down_revision = "0091_sync_dispatch_lifetime"
branch_labels = None
depends_on = None

TABLES = (
    (
        "report_sync_job",
        "finished_at",
        "ix_report_summary_archive",
        "'COMPLETE','FAILED','CANCELLED'",
    ),
    (
        "report_balance_run",
        "completed_at",
        "ix_balance_summary_archive",
        "'COMPLETE','FAILED','CANCELLED'",
    ),
    (
        "ad_directory_run",
        "completed_at",
        "ix_directory_summary_archive",
        "'COMPLETE','FAILED','CANCELLED','STALE'",
    ),
)


def upgrade():
    op.add_column(
        "report_balance_run", sa.Column("completed_at", sa.DateTime(timezone=True))
    )
    for table, timestamp, index, terminal in TABLES:
        # 没有旧终结时间的记录从本次观察起保留90天，不能拿created_at伪造早已过期。
        op.execute(
            sa.text(
                f"UPDATE {table} SET {timestamp}=CURRENT_TIMESTAMP WHERE status IN ({terminal}) AND {timestamp} IS NULL"
            )
        )
        op.create_index(
            index,
            table,
            ["tenant_id", timestamp, "id"],
            postgresql_where=sa.text(f"status IN ({terminal})"),
        )
    op.create_index(
        "ix_directory_material_source_run",
        "ad_directory_run",
        ["tenant_id", sa.text("(query->'material_child_v1'->>'source_run_id')")],
        postgresql_where=sa.text("query ? 'material_child_v1'"),
    )


def downgrade():
    op.drop_index("ix_directory_material_source_run", table_name="ad_directory_run")
    for table, _timestamp, index, _terminal in reversed(TABLES):
        op.drop_index(index, table_name=table)
    op.drop_column("report_balance_run", "completed_at")
