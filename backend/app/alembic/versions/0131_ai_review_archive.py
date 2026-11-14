"""Durable task-scoped evidence, delivered receipts and analysis artifacts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0131_ai_review_archive"
down_revision = "0130_ai_review_reading"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_ai_dsh_review_scope",
        "ai_dsh_review_task",
        ["tenant_id", "optimizer_id", "id"],
    )
    for table, extra in (
        (
            "ai_review_source",
            [
                sa.Column("category", sa.String(40), nullable=False),
                sa.Column("advertiser_id", sa.String(100)),
                sa.Column("dimension", sa.String(20)),
                sa.Column("maturity_horizon", sa.String(5)),
                sa.Column("digest", sa.String(64), nullable=False),
                sa.Column("payload", postgresql.JSONB(), nullable=False),
                sa.Column("source_metadata", postgresql.JSONB(), nullable=False),
                sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            ],
        ),
        (
            "ai_review_receipt",
            [
                sa.Column("source_id", sa.Uuid(), nullable=False),
                sa.Column("source_digest", sa.String(64), nullable=False),
                sa.Column("payload", postgresql.JSONB(), nullable=False),
                sa.Column("citable_ids", postgresql.JSONB(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            ],
        ),
        (
            "ai_review_artifact",
            [
                sa.Column("kind", sa.String(30), nullable=False),
                sa.Column("key", sa.String(100), nullable=False),
                sa.Column("digest", sa.String(64), nullable=False),
                sa.Column("payload", postgresql.JSONB(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            ],
        ),
    ):
        op.create_table(
            table,
            sa.Column("task_id", sa.Uuid(), primary_key=True),
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("optimizer_id", sa.Uuid(), nullable=False),
            *extra,
            sa.ForeignKeyConstraint(
                ["tenant_id", "optimizer_id", "task_id"],
                [
                    "ai_dsh_review_task.tenant_id",
                    "ai_dsh_review_task.optimizer_id",
                    "ai_dsh_review_task.id",
                ],
                name=f"fk_{table}_task",
                ondelete="CASCADE",
            ),
        )
    op.create_index(
        "ix_ai_review_source_lookup",
        "ai_review_source",
        ["task_id", "category", "advertiser_id", "dimension", "maturity_horizon"],
    )
    op.create_index(
        "ix_ai_review_artifact_lookup",
        "ai_review_artifact",
        ["task_id", "kind", "key", "created_at"],
    )
    # 来源、交付凭证与每个笔记版本均追加写入；数据库拒绝绕过服务覆盖历史。
    op.execute("""CREATE FUNCTION ai_review_archive_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'review archive rows are immutable' USING ERRCODE = '23514'; END;
    $$""")
    for table in ("ai_review_source", "ai_review_receipt", "ai_review_artifact"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION ai_review_archive_immutable()"
        )
    # 不重放旧会话；已完成历史冻结 JSON 原样保留。部署先正常排空消费者。
    op.execute(
        "UPDATE ai_dsh_review_task SET status='CANCELLED', cancellation_requested=true, finished_at=now() WHERE status IN ('PENDING','RUNNING')"
    )


def downgrade():
    for table in ("ai_review_artifact", "ai_review_receipt", "ai_review_source"):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS ai_review_archive_immutable()")
    op.drop_constraint("uq_ai_dsh_review_scope", "ai_dsh_review_task", type_="unique")
