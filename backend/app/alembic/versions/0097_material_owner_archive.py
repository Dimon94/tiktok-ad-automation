"""Retain a narrow owner observation bound after material child pages expire."""

import sqlalchemy as sa
from alembic import op

revision = "0097_material_owner_archive"
down_revision = "0096_control_wake_compaction"
branch_labels = None
depends_on = None


def upgrade():
    # 旧运行不伪造观测；逐运行显式归档时保守补登记，不在DDL中扫全部owner历史。
    op.add_column(
        "ad_directory_run",
        sa.Column(
            "material_owner_observed_through", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade():
    op.drop_column("ad_directory_run", "material_owner_observed_through")
