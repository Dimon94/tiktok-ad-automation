"""Unify video lookup and full identity so skew cannot choose a narrow cached plan."""

import sqlalchemy as sa
from alembic import op

revision = "0094_material_video_identity"
down_revision = "0093_sync_archive_finalization"
branch_labels = None
depends_on = None


def upgrade():
    # 停写迁移内原子替换；去掉本候选0083的重复索引，避免新增一套长期写放大。
    op.drop_index("ix_ad_material_video", table_name="ad_material_reference")
    op.drop_index(
        "ix_ad_material_complete_identity", table_name="ad_material_reference"
    )
    op.create_index(
        "ix_ad_material_video",
        "ad_material_reference",
        [
            "tenant_id",
            "platform_material_id",
            "advertiser_id",
            "ad_remote_id",
            "material_type",
        ],
        postgresql_where=sa.text("complete"),
    )


def downgrade():
    op.drop_index("ix_ad_material_video", table_name="ad_material_reference")
    op.create_index(
        "ix_ad_material_video",
        "ad_material_reference",
        ["tenant_id", "platform_material_id"],
        postgresql_where=sa.text("complete"),
    )
    op.create_index(
        "ix_ad_material_complete_identity",
        "ad_material_reference",
        [
            "tenant_id",
            "advertiser_id",
            "ad_remote_id",
            "platform_material_id",
            "material_type",
        ],
        postgresql_where=sa.text("complete"),
    )
