"""Bound active directory candidate lookup before locking the wide execution."""

import sqlalchemy as sa
from alembic import op

revision = "0098_directory_pending_order"
down_revision = "0097_material_owner_archive"
branch_labels = None
depends_on = None


def replace_index(columns):
    op.drop_index("ix_ad_directory_pending_account", table_name="ad_directory_run")
    op.create_index(
        "ix_ad_directory_pending_account",
        "ad_directory_run",
        columns,
        postgresql_where=sa.text(
            "status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')"
        ),
    )


def upgrade():
    # 合并同前缀索引；有序窄候选不需要为取一项反复排序整个账户积压。
    replace_index(["tenant_id", "advertiser_id", "created_at", "id"])


def downgrade():
    replace_index(["tenant_id", "advertiser_id"])
