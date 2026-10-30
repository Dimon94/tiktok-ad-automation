"""Explicit V2 dispatch lifetime; legacy messages retain their prior contract."""

import sqlalchemy as sa
from alembic import op

revision = "0091_sync_dispatch_lifetime"
down_revision = "0090_pool_readiness_cursor"
branch_labels = None
depends_on = None


def _pending_indexes(with_retirement):
    condition = "published_at IS NULL AND superseded_at IS NULL"
    if with_retirement:
        condition += " AND retired_at IS NULL"
    for name, columns, suffix in (
        ("ix_dispatch_pending", ["available_at", "id"], ""),
        ("ix_dispatch_tenant_pending", ["tenant_id", "available_at", "id"], ""),
        (
            "ix_dispatch_expansion_pending",
            ["tenant_id", "available_at", "id"],
            " AND task_name = 'builds.expand_submission'",
        ),
    ):
        op.drop_index(name, table_name="pending_dispatch")
        op.create_index(
            name,
            "pending_dispatch",
            columns,
            postgresql_where=sa.text(condition + suffix),
        )


def _compaction_index(with_targeted):
    tasks = "'reporting.job_step','reporting.backfill_step','ads.sync_step',"
    if with_targeted:
        tasks += "'ads.targeted_step',"
    tasks += "'ads.publish_step','reporting.balance_step'"
    op.drop_index("ix_dispatch_sync_compaction", table_name="pending_dispatch")
    op.create_index(
        "ix_dispatch_sync_compaction",
        "pending_dispatch",
        ["published_at", "id"],
        postgresql_where=sa.text(
            "published_at IS NOT NULL AND compacted_at IS NULL AND task_name IN ("
            + tasks
            + ")"
        ),
    )


def upgrade():
    op.add_column(
        "pending_dispatch", sa.Column("dispatch_contract_version", sa.Integer())
    )
    for name in ("issued_at", "valid_until", "retired_at"):
        op.add_column("pending_dispatch", sa.Column(name, sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_dispatch_lifetime_contract",
        "pending_dispatch",
        "(dispatch_contract_version IS NULL AND issued_at IS NULL AND valid_until IS NULL AND retired_at IS NULL) "
        "OR (dispatch_contract_version IS NOT NULL AND dispatch_contract_version=2 AND issued_at IS NOT NULL AND valid_until IS NOT NULL "
        "AND valid_until>issued_at AND (retired_at IS NULL OR retired_at>=issued_at))",
    )
    _pending_indexes(True)
    _compaction_index(True)
    op.create_index(
        "ix_dispatch_v2_expiry",
        "pending_dispatch",
        ["valid_until", "id"],
        postgresql_where=sa.text("dispatch_contract_version=2 AND retired_at IS NULL"),
    )
    op.create_index(
        "ix_dispatch_v2_retention",
        "pending_dispatch",
        ["retired_at", "id"],
        postgresql_where=sa.text("dispatch_contract_version=2"),
    )


def downgrade():
    op.drop_index("ix_dispatch_v2_retention", table_name="pending_dispatch")
    op.drop_index("ix_dispatch_v2_expiry", table_name="pending_dispatch")
    _compaction_index(False)
    _pending_indexes(False)
    op.drop_constraint(
        "ck_dispatch_lifetime_contract", "pending_dispatch", type_="check"
    )
    for name in ("retired_at", "valid_until", "issued_at", "dispatch_contract_version"):
        op.drop_column("pending_dispatch", name)
