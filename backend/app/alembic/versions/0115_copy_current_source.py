"""复制按当前广告事实冻结，本地策略/版权方/文案关联不再是准入条件。"""

from typing import cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0115_copy_current_source"
down_revision = "0114_retire_material"
branch_labels = None
depends_on = None


def upgrade():
    for table, columns in {
        "build_draft": (
            "strategy_version_id",
            "provider_connection_id",
            "application_id",
        ),
        "build_preview": ("strategy_version_id",),
        "preview_drama": ("link_id",),
        "planned_ad": ("copy_id",),
    }.items():
        for column in columns:
            op.alter_column(table, column, nullable=True)
    # 预览内分组身份和版权方真实身份分离；历史值原样迁移，不伪造版权方。
    op.add_column(
        "preview_drama", sa.Column("provider_drama_id", sa.Uuid(), nullable=True)
    )
    op.execute("ALTER TABLE preview_drama DISABLE TRIGGER preview_drama_frozen")
    op.execute("UPDATE preview_drama SET provider_drama_id=drama_id")
    op.execute("ALTER TABLE preview_drama ENABLE TRIGGER preview_drama_frozen")
    conn = op.get_bind()
    for fk in sa.inspect(conn).get_foreign_keys("preview_drama"):
        if fk["referred_table"] == "provider_drama":
            op.drop_constraint(
                cast(str, fk["name"]), "preview_drama", type_="foreignkey"
            )
    op.create_foreign_key(
        "fk_preview_provider_drama",
        "preview_drama",
        "provider_drama",
        ["tenant_id", "provider_drama_id"],
        ["tenant_id", "id"],
    )
    for fk in sa.inspect(conn).get_foreign_keys("preview_creative_unit"):
        if fk["referred_table"] == "provider_drama":
            op.drop_constraint(
                cast(str, fk["name"]), "preview_creative_unit", type_="foreignkey"
            )
    # 普通预览先跨事务复制创意，再分页冻结剧目；完整性应在最终FROZEN边界检查。
    definition = conn.execute(
        sa.text(
            "SELECT pg_get_functiondef('check_preview_v2_complete()'::regprocedure)"
        )
    ).scalar_one()
    marker = "IF EXISTS ("
    replacement = """IF EXISTS (SELECT 1 FROM preview_creative_unit u WHERE u.tenant_id=NEW.tenant_id AND u.preview_id=NEW.id
        AND NOT EXISTS(SELECT 1 FROM preview_drama d WHERE d.tenant_id=u.tenant_id AND d.preview_id=u.preview_id AND d.drama_id=u.drama_id)) THEN
        RAISE EXCEPTION 'frozen creative source missing'; END IF;
          IF EXISTS ("""
    if marker not in definition:
        raise RuntimeError("unexpected frozen creative integrity contract")
    op.execute(definition.replace(marker, replacement, 1))

    op.create_check_constraint(
        "ck_draft_source_references",
        "build_draft",
        "COALESCE((strategy_version_id IS NOT NULL AND provider_connection_id IS NOT NULL AND application_id IS NOT NULL) OR (link_config->>'source_mode'='CAMPAIGN_COPY' AND strategy_version_id IS NULL AND provider_connection_id IS NULL AND application_id IS NULL),false)",
    )
    op.create_check_constraint(
        "ck_preview_source_strategy",
        "build_preview",
        "strategy_version_id IS NOT NULL OR COALESCE(config->>'campaign_copy_version'='2',false)",
    )
    op.execute("""
      CREATE FUNCTION check_copy_source_references() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE is_copy boolean;
      BEGIN
        SELECT config->>'campaign_copy_version'='2' INTO is_copy FROM build_preview
          WHERE tenant_id=NEW.tenant_id AND id=NEW.preview_id;
        IF TG_TABLE_NAME='preview_drama' THEN
          IF COALESCE(is_copy,false) THEN
            IF NEW.provider_drama_id IS NOT NULL OR NEW.link_id IS NOT NULL THEN
              RAISE EXCEPTION 'copy preview cannot invent provider references';
            END IF;
          ELSE
            IF NEW.provider_drama_id IS NULL THEN NEW.provider_drama_id:=NEW.drama_id; END IF;
            IF NEW.provider_drama_id IS DISTINCT FROM NEW.drama_id OR NEW.link_id IS NULL THEN
              RAISE EXCEPTION 'ordinary preview requires provider references';
            END IF;
          END IF;
        ELSIF NEW.copy_id IS NULL AND NOT COALESCE(is_copy,false) THEN
          RAISE EXCEPTION 'ordinary ad requires copy pool reference';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER preview_drama_source_references BEFORE INSERT ON preview_drama FOR EACH ROW EXECUTE FUNCTION check_copy_source_references();
      CREATE TRIGGER planned_ad_source_references BEFORE INSERT ON planned_ad FOR EACH ROW EXECUTE FUNCTION check_copy_source_references();
    """)
    op.create_table(
        "campaign_copy_source_facts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("source", postgresql.JSONB(), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "dispatch_id",
            sa.Uuid(),
            sa.ForeignKey("pending_dispatch.id"),
            nullable=True,
        ),
        sa.Column("repair_after", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        sa.UniqueConstraint("tenant_id", "source_digest", name="uq_copy_source_facts"),
        sa.CheckConstraint(
            "status IN ('PENDING','READY','FAILED')", name="ck_copy_source_facts_status"
        ),
    )
    op.create_index(
        "ix_copy_source_facts_repair",
        "campaign_copy_source_facts",
        ["status", "repair_after", "id"],
    )


def downgrade():
    raise RuntimeError("复制已可不依赖版权方及策略，不能自动降级丢弃来源事实")
