"""汇合 Native 冻结报表与上游素材链接迁移，保留两侧历史结构。"""

revision = "0133_native_material_merge"
down_revision = ("0132_native_ai_archive_merge", "0132_material_link_upload")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
