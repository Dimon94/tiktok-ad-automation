"""原生目的地原子选择；历史 Mini 与冻结事实保持原文。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "native_series_selection"
down_revision = "0118_sync_schedule_service_age"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "build_draft",
        sa.Column(
            "scene_selection", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )
    op.add_column(
        "build_scene_job",
        sa.Column(
            "native_selection", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )
    for table, name, expression in (
        (
            "build_scene_job",
            "ck_build_scene_job_resource",
            "resource IN ('capabilities','native_series','native_tt_identity','native_bc_identity','identity','minis','cta','vbo','regions','budget','done')",
        ),
        (
            "build_scene_job_page",
            "ck_build_scene_job_page_resource",
            "resource IN ('native_series','native_tt_identity','native_bc_identity','identity','minis','cta','vbo','regions','budget')",
        ),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)
    op.drop_constraint("ck_draft_source_references", "build_draft", type_="check")
    op.create_check_constraint(
        "ck_draft_source_references",
        "build_draft",
        "COALESCE((scene_selection->>'kind'='NATIVE_SERIES_DIRECT_VIDEO' AND strategy_version_id IS NOT NULL AND provider_connection_id IS NULL AND application_id IS NULL AND link_config='{}'::jsonb AND media_kind='VIDEO' AND identity_id IS NULL) OR ((scene_selection IS NULL OR scene_selection->>'kind'='MINI_APP') AND ((strategy_version_id IS NOT NULL AND provider_connection_id IS NOT NULL AND application_id IS NOT NULL) OR (link_config->>'source_mode'='CAMPAIGN_COPY' AND strategy_version_id IS NULL AND provider_connection_id IS NULL AND application_id IS NULL))),false)",
    )


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM build_scene_job WHERE native_selection IS NOT NULL) OR EXISTS (SELECT 1 FROM build_draft WHERE scene_selection IS NOT NULL) THEN RAISE EXCEPTION 'Cannot discard destination selections'; END IF; END $$"
    )
    op.drop_constraint("ck_draft_source_references", "build_draft", type_="check")
    op.create_check_constraint(
        "ck_draft_source_references",
        "build_draft",
        "COALESCE((strategy_version_id IS NOT NULL AND provider_connection_id IS NOT NULL AND application_id IS NOT NULL) OR (link_config->>'source_mode'='CAMPAIGN_COPY' AND strategy_version_id IS NULL AND provider_connection_id IS NULL AND application_id IS NULL),false)",
    )
    for table, name, expression in (
        (
            "build_scene_job",
            "ck_build_scene_job_resource",
            "resource IN ('capabilities','identity','minis','cta','vbo','regions','budget','done')",
        ),
        (
            "build_scene_job_page",
            "ck_build_scene_job_page_resource",
            "resource IN ('identity','minis','cta','vbo','regions','budget')",
        ),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)
    op.drop_column("build_scene_job", "native_selection")
    op.drop_column("build_draft", "scene_selection")
