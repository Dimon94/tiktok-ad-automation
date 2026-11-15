"""Move mutable draft identity into its single destination selection owner."""

import sqlalchemy as sa
from alembic import op

revision = "native_series_identity"
down_revision = "native_series_selection"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE build_draft SET scene_selection=jsonb_build_object('kind','MINI_APP','creative_identity',jsonb_strip_nulls(jsonb_build_object('identity_id',identity_id,'identity_type',identity_type,'identity_authorized_bc_id',identity_authorized_bc_id))) WHERE identity_id IS NOT NULL"
    )
    op.drop_constraint("ck_build_draft_identity", "build_draft", type_="check")
    op.drop_constraint("ck_draft_source_references", "build_draft", type_="check")
    op.create_check_constraint(
        "ck_draft_source_references",
        "build_draft",
        "COALESCE((scene_selection->>'kind'='NATIVE_SERIES_DIRECT_VIDEO' AND strategy_version_id IS NOT NULL AND provider_connection_id IS NULL AND application_id IS NULL AND link_config='{}'::jsonb AND media_kind='VIDEO') OR ((scene_selection IS NULL OR scene_selection->>'kind'='MINI_APP') AND ((strategy_version_id IS NOT NULL AND provider_connection_id IS NOT NULL AND application_id IS NOT NULL) OR (link_config->>'source_mode'='CAMPAIGN_COPY' AND strategy_version_id IS NULL AND provider_connection_id IS NULL AND application_id IS NULL))),false)",
    )
    for name in ("identity_id", "identity_type", "identity_authorized_bc_id"):
        op.drop_column("build_draft", name)


def downgrade():
    for name, size in (
        ("identity_id", 255),
        ("identity_type", 32),
        ("identity_authorized_bc_id", 255),
    ):
        op.add_column("build_draft", sa.Column(name, sa.String(size), nullable=True))
    op.execute(
        "UPDATE build_draft SET identity_id=scene_selection->'creative_identity'->>'identity_id', identity_type=scene_selection->'creative_identity'->>'identity_type',identity_authorized_bc_id=scene_selection->'creative_identity'->>'identity_authorized_bc_id' WHERE scene_selection->>'kind'='MINI_APP'"
    )
    op.execute(
        "UPDATE build_draft SET scene_selection=NULL WHERE scene_selection->>'kind'='MINI_APP'"
    )
    op.create_check_constraint(
        "ck_build_draft_identity",
        "build_draft",
        "(identity_id IS NULL AND identity_type IS NULL AND identity_authorized_bc_id IS NULL) OR (identity_id IS NOT NULL AND identity_type IN ('TT_USER','BC_AUTH_TT') AND ((identity_type='TT_USER' AND identity_authorized_bc_id IS NULL) OR (identity_type='BC_AUTH_TT' AND identity_authorized_bc_id IS NOT NULL)))",
    )
