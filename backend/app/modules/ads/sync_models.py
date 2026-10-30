"""采集运行与暂存页；这里只定义持久合同，完整发布由同步服务负责。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Identity,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime, account_reference
from app.modules.materials.routes import route_constraint


def frozen_scope_constraints(table: str) -> tuple:
    return (
        account_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        # 平台管理员可无 tenant_membership 代管租户；操作人只关联用户，
        # 权限在每次执行时由 require_tenant 重新校验。
        ForeignKeyConstraint(["actor_id"], ["user.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "channel"],
            [
                "tiktok_connection.tenant_id",
                "tiktok_connection.id",
                "tiktok_connection.kind",
            ],
        ),
        route_constraint(table, "frozen_route", connection=True),
        CheckConstraint(
            "frozen_route ? 'binding_revision' AND frozen_route->>'channel' = channel",
            name=f"ck_{table}_route_generation",
        ),
    )


def run_constraints(table: str) -> tuple:
    return (
        *frozen_scope_constraints(table),
        UniqueConstraint("tenant_id", "advertiser_id", "id", name=f"uq_{table}_scope"),
        UniqueConstraint(
            "tenant_id",
            "request_id",
            "advertiser_id",
            "partition_key",
            name=f"uq_{table}_request",
        ),
        CheckConstraint(
            "claim_generation >= 0 AND next_page > 0 AND "
            "(published_version IS NULL OR published_version > 0)",
            name=f"ck_{table}_numbers",
        ),
        CheckConstraint(
            "partition_key ~ '^[0-9a-f]{64}$'", name=f"ck_{table}_partition"
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE','COMPLETE','FAILED','CANCELLED','STALE')",
            name=f"ck_{table}_status",
        ),
        Index(f"ix_{table}_due", "status", "next_attempt_at", "claimed_until"),
        Index(
            f"ix_{table}_partition",
            "tenant_id",
            "advertiser_id",
            "partition_key",
            "request_sequence",
        ),
    )


class FrozenScope(SQLModel):
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    bc_id: str = Field(max_length=128)
    actor_id: UUID
    connection_id: UUID
    channel: str = Field(default="OFFICIAL_API", max_length=32)
    frozen_route: dict[str, Any] = Field(sa_type=JSONB, nullable=False)


class SyncRunFields(FrozenScope):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    request_id: UUID = Field(default_factory=uuid4)
    # 完整 query（排除 page）规范序列化后的摘要，必须含 ID 分片及指标组。
    partition_key: str = Field(max_length=64)
    query: dict[str, Any] = Field(sa_type=JSONB, nullable=False)
    status: str = Field(default="QUEUED", max_length=32)
    claim_generation: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(default=None, sa_type=AwareDateTime)
    next_attempt_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    next_page: int = 1
    observed_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    coverage: str = Field(default="PENDING", max_length=32)
    missing_reason: str | None = None
    error_code: str | None = None
    published_version: int | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    completed_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class AdDirectoryRun(SyncRunFields, table=True):
    owner_epoch: int = Field(default=1, sa_type=BigInteger)
    contract_version: int = 2
    # 分页领取代数用于隔离旧消息；发布失败预算独立累计，不能把页数当失败数。
    publish_failures: int = 0
    # 素材子范围冻结owner观察的保守上界，页回收后仍可证明旧子已被新观察替换。
    material_owner_observed_through: datetime | None = Field(
        default=None, sa_type=AwareDateTime
    )
    __tablename__ = "ad_directory_run"
    __table_args__ = (
        # 归档只查同范围或完整快照的充分覆盖证据；避免逐历史运行扫描JSON。
        Index(
            "ix_directory_archive_partition_cover",
            "tenant_id",
            "advertiser_id",
            "bc_id",
            "actor_id",
            "kind",
            "ad_type",
            text("md5(frozen_route::text)"),
            "partition_key",
            "request_sequence",
            postgresql_where=text(
                "published_version IS NOT NULL AND NOT (query ? 'material_child_v1')"
            ),
        ),
        Index(
            "ix_directory_archive_full_cover",
            "tenant_id",
            "advertiser_id",
            "bc_id",
            "actor_id",
            "kind",
            "ad_type",
            text("md5(frozen_route::text)"),
            "request_sequence",
            postgresql_where=text(
                "published_version IS NOT NULL AND NOT (query ? 'material_child_v1') AND coalesce(query->'ids','[]'::jsonb)='[]'::jsonb AND coalesce(query->'parent_ids','[]'::jsonb)='[]'::jsonb AND query->>'include_deleted'='true'"
            ),
        ),
        Index(
            "ix_directory_material_source_run",
            "tenant_id",
            text("((query -> 'material_child_v1'::text) ->> 'source_run_id'::text)"),
            postgresql_where=text("query ? 'material_child_v1'"),
        ),
        Index(
            "ix_directory_summary_archive",
            "tenant_id",
            "completed_at",
            "id",
            postgresql_where=text(
                "status IN ('COMPLETE','FAILED','CANCELLED','STALE')"
            ),
        ),
        CheckConstraint("publish_failures >= 0", name="ck_directory_publish_failures"),
        *run_constraints("ad_directory_run"),
        # 单户在途探测不应为证明“没有积压”读取该户全部终态历史。
        Index(
            "ix_ad_directory_pending_account",
            "tenant_id",
            "advertiser_id",
            "created_at",
            "id",
            postgresql_where=text(
                "status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')"
            ),
        ),
        Index(
            "ix_ad_directory_recent_success",
            "tenant_id",
            "advertiser_id",
            "completed_at",
            postgresql_where=text(
                "status = 'COMPLETE' AND CAST(query->>'include_deleted' AS boolean) IS TRUE"
            ),
        ),
        CheckConstraint(
            "workload IN ('live','background')", name="ck_ad_directory_workload"
        ),
        CheckConstraint(
            "kind IN ('campaign','adgroup','ad','creative')",
            name="ck_ad_directory_run_kind",
        ),
    )
    request_sequence: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger, Identity(always=True), nullable=False, unique=True
        ),
    )
    workload: str = Field(default="live", max_length=16)
    kind: str = Field(max_length=16)
    ad_type: str = Field(max_length=64)


def page_constraints(table: str, run_table: str) -> tuple:
    return (
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id", "run_id"],
            [f"{run_table}.tenant_id", f"{run_table}.advertiser_id", f"{run_table}.id"],
        ),
        CheckConstraint(
            "page > 0 AND claim_generation > 0 AND (next_page IS NULL OR next_page > page) "
            "AND (NOT complete OR next_page IS NULL)",
            name=f"ck_{table}_numbers",
        ),
        Index(f"ix_{table}_scope", "tenant_id", "advertiser_id", "run_id"),
    )


class StagedPageFields(SQLModel):
    run_id: UUID = Field(primary_key=True)
    page: int = Field(primary_key=True)
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    claim_generation: int
    next_page: int | None = None
    complete: bool
    evidence: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    coverage: str = Field(default="PENDING", max_length=32)
    missing_reason: str | None = None
    error_code: str | None = None
    published_version: int | None = None


class AdDirectoryPage(StagedPageFields, table=True):
    __tablename__ = "ad_directory_page"
    __table_args__ = page_constraints("ad_directory_page", "ad_directory_run")
    items: list[dict[str, Any]] = Field(sa_type=JSONB, nullable=False)
    materials: list[dict[str, Any]] = Field(sa_type=JSONB, nullable=False)
