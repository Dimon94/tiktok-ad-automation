from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class PendingDispatch(SQLModel, table=True):
    __tablename__ = "pending_dispatch"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_key", name="uq_dispatch_tenant_key"),
        Index(
            "ix_dispatch_material_operation",
            "tenant_id",
            text("(payload ->> 'operation_id')"),
            "task_name",
            postgresql_where=text(
                "task_name IN ('materials.upload_original','materials.verify_original',"
                "'materials.prepare_target','materials.verify_target')"
            ),
        ),
        Index(
            "ix_dispatch_pending",
            "available_at",
            "id",
            postgresql_where=text(
                "published_at IS NULL AND superseded_at IS NULL AND retired_at IS NULL"
            ),
        ),
        Index(
            "ix_dispatch_tenant_pending",
            "tenant_id",
            "available_at",
            "id",
            postgresql_where=text(
                "published_at IS NULL AND superseded_at IS NULL AND retired_at IS NULL"
            ),
        ),
        Index(
            "ix_dispatch_expansion_pending",
            "tenant_id",
            "available_at",
            "id",
            postgresql_where=text(
                "published_at IS NULL AND superseded_at IS NULL AND retired_at IS NULL AND task_name = 'builds.expand_submission'"
            ),
        ),
        Index(
            "ix_dispatch_compaction",
            "published_at",
            "id",
            postgresql_where=text(
                "published_at IS NOT NULL AND compacted_at IS NULL "
                "AND task_name IN ('builds.execute_step','builds.execute_unit')"
            ),
        ),
        *(
            Index(
                f"ix_dispatch_uncompacted_{key}",
                "tenant_id",
                text(f"(payload ->> '{key}')"),
                postgresql_where=text(
                    f"compacted_at IS NULL AND payload ->> '{key}' IS NOT NULL"
                ),
            )
            for key in ("job_id", "run_id")
        ),
        Index(
            "ix_dispatch_pure_wake_compaction",
            "published_at",
            "id",
            postgresql_where=text(
                "published_at IS NOT NULL AND compacted_at IS NULL AND task_name IN ('reporting.plan_due','reporting.plan_continue')"
            ),
        ),
        Index(
            "ix_dispatch_sync_compaction",
            "published_at",
            "id",
            postgresql_where=text(
                "published_at IS NOT NULL AND compacted_at IS NULL AND task_name IN ('reporting.job_step','reporting.backfill_step','ads.sync_step','ads.targeted_step','ads.publish_step','reporting.balance_step')"
            ),
        ),
        Index(
            "ix_dispatch_superseded_compaction",
            "superseded_at",
            "id",
            postgresql_where=text("superseded_at IS NOT NULL AND compacted_at IS NULL"),
        ),
        CheckConstraint(
            "(dispatch_contract_version IS NULL AND issued_at IS NULL AND valid_until IS NULL AND retired_at IS NULL) "
            "OR (dispatch_contract_version IS NOT NULL AND dispatch_contract_version=2 AND issued_at IS NOT NULL AND valid_until IS NOT NULL "
            "AND valid_until>issued_at AND (retired_at IS NULL OR retired_at>=issued_at))",
            name="ck_dispatch_lifetime_contract",
        ),
        Index(
            "ix_dispatch_v2_expiry",
            "valid_until",
            "id",
            postgresql_where=text("dispatch_contract_version=2 AND retired_at IS NULL"),
        ),
        Index(
            "ix_dispatch_v2_retention",
            "retired_at",
            "id",
            postgresql_where=text("dispatch_contract_version=2"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    actor_id: UUID
    task_name: str = Field(max_length=255)
    task_key: str = Field(max_length=255)
    payload: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    # Legacy摘要永久保留；显式V2消息由已验证的寿命/消费围栏安全回收。
    payload_digest: str | None = Field(default=None, max_length=64)
    compacted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    available_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
        ),
    )
    published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # 离线合同迁移明确退役的消息保留原正文/摘要，不冒充已交给broker。
    superseded_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    dispatch_contract_version: int | None = None
    issued_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    valid_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    retired_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    attempts: int = Field(default=0)


class DispatchTenantCursor(SQLModel, table=True):
    __tablename__ = "dispatch_tenant_cursor"

    tenant_id: UUID = Field(primary_key=True)
    last_published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
