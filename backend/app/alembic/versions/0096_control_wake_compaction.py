"""Include expiring planning continuations in bounded legacy wake compaction."""

import sqlalchemy as sa
from alembic import op

revision = "0096_control_wake_compaction"
down_revision = "0095_directory_archive_cover"
branch_labels = None
depends_on = None


def replace_index(tasks):
    op.drop_index("ix_dispatch_pure_wake_compaction", table_name="pending_dispatch")
    op.create_index(
        "ix_dispatch_pure_wake_compaction",
        "pending_dispatch",
        ["published_at", "id"],
        postgresql_where=sa.text(
            "published_at IS NOT NULL AND compacted_at IS NULL AND " + tasks
        ),
    )


def upgrade():
    replace_index("task_name IN ('reporting.plan_due','reporting.plan_continue')")


def downgrade():
    replace_index("task_name='reporting.plan_due'")
