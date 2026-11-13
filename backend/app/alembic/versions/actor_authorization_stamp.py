"""Persist authority generations and immutable submission approval stamps."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "actor_authorization_stamp"
down_revision = "native_batch_preview"
branch_labels = None
depends_on = None


def upgrade():
    # 全局 sequence 只分配唯一代数；比较仍局限实际 actor/tenant/member，不全局失效。
    # 删除重建也取得新代数，避免 membership ABA；所有 SQL 写入口遵循同一事实 owner。
    op.execute("CREATE SEQUENCE authority_generation_seq AS bigint")
    for table, fields in (
        ("user", ("is_active", "is_superuser")),
        ("tenant", ("active",)),
        ("tenant_membership", ("active", "role")),
    ):
        op.add_column(
            table,
            sa.Column(
                "authorization_generation",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("nextval('authority_generation_seq')"),
            ),
        )
        changed = " OR ".join(
            f"NEW.{field} IS DISTINCT FROM OLD.{field}" for field in fields
        )
        op.execute(f"""
        CREATE FUNCTION {table}_authority_generation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            NEW.authorization_generation := nextval('authority_generation_seq');
          ELSIF {changed} THEN
            NEW.authorization_generation := nextval('authority_generation_seq');
          ELSE
            NEW.authorization_generation := OLD.authorization_generation;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER authority_generation BEFORE INSERT OR UPDATE ON "{table}"
          FOR EACH ROW EXECUTE FUNCTION {table}_authority_generation();
        """)
    # 历史批准未知，保持 NULL；不能把升级当成操作者重新批准。
    op.add_column(
        "build_submission", sa.Column("authorization_stamp", JSONB(), nullable=True)
    )
    op.execute("""
    CREATE FUNCTION submission_authorization_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.authorization_stamp::jsonb IS DISTINCT FROM OLD.authorization_stamp::jsonb THEN
        RAISE EXCEPTION 'immutable submission authorization' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER submission_authorization_immutable BEFORE UPDATE ON build_submission
      FOR EACH ROW EXECUTE FUNCTION submission_authorization_immutable();
    """)


def downgrade():
    raise RuntimeError("Approval generations cannot be rolled back or reused")
