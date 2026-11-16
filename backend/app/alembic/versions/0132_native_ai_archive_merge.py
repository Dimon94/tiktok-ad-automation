"""汇合 Native 集成与 AI v2 不可变归档迁移；两侧原迁移保持不变。"""

revision = "0132_native_ai_archive_merge"
down_revision = ("0131_native_ai_merge", "0131_ai_review_archive")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
