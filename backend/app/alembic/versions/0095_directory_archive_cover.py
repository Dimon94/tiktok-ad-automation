"""Bound archive coverage proof to indexed exact or full snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0095_directory_archive_cover"
down_revision = "0094_material_video_identity"
branch_labels = None
depends_on = None

LEADING = [
    "tenant_id",
    "advertiser_id",
    "bc_id",
    "actor_id",
    "kind",
    "ad_type",
    sa.text("md5(frozen_route::text)"),
]
PUBLISHED = "published_version IS NOT NULL AND NOT (query ? 'material_child_v1')"
FULL = (
    PUBLISHED
    + " AND coalesce(query->'ids','[]'::jsonb)='[]'::jsonb AND coalesce(query->'parent_ids','[]'::jsonb)='[]'::jsonb AND query->>'include_deleted'='true'"
)


def upgrade():
    # 路由hash只用于索引，查询仍复核完整JSON，不能把hash当授权身份。
    op.create_index(
        "ix_directory_archive_partition_cover",
        "ad_directory_run",
        [*LEADING, "partition_key", "request_sequence"],
        postgresql_where=sa.text(PUBLISHED),
    )
    op.create_index(
        "ix_directory_archive_full_cover",
        "ad_directory_run",
        [*LEADING, "request_sequence"],
        postgresql_where=sa.text(FULL),
    )


def downgrade():
    op.drop_index("ix_directory_archive_full_cover", table_name="ad_directory_run")
    op.drop_index("ix_directory_archive_partition_cover", table_name="ad_directory_run")
