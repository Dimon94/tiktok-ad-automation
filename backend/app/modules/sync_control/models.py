"""控制表不保存第二份平台页；执行仍引用各业务原有运行记录。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime
from app.modules.ads.sync_models import FrozenScope, frozen_scope_constraints


class SyncScope(FrozenScope, table=True):
    __tablename__ = "sync_scope"
    __table_args__ = (
        *frozen_scope_constraints("sync_scope"),
        UniqueConstraint("identity_digest", name="uq_sync_scope_identity"),
        UniqueConstraint("id", "tenant_id", name="uq_sync_scope_tenant"),
        CheckConstraint(
            "owner_epoch > 0 AND contract_version = 2", name="ck_sync_scope_fence"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    resource: str = Field(max_length=64)
    timezone: str = Field(max_length=128)
    identity_digest: str = Field(max_length=64)
    query_contract: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    owner_epoch: int = 1
    contract_version: int = 2
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )


class SyncRequirement(SQLModel, table=True):
    __tablename__ = "sync_requirement"
    __table_args__ = (
        ForeignKeyConstraint(
            ["scope_id", "tenant_id"],
            ["sync_scope.id", "sync_scope.tenant_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("scope_id", "bucket_key", name="uq_sync_requirement_bucket"),
        UniqueConstraint(
            "id", "scope_id", "tenant_id", name="uq_sync_requirement_scope"
        ),
        CheckConstraint(
            "desired_revision > 0 AND satisfied_revision >= 0 AND satisfied_revision <= desired_revision",
            name="ck_sync_requirement_revision",
        ),
        Index(
            "ix_sync_requirement_pending",
            "scope_id",
            "oldest_pending_at",
            postgresql_where=text("satisfied_revision < desired_revision"),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    scope_id: UUID
    bucket_key: str = Field(max_length=256)
    bucket: dict[str, Any] = Field(sa_type=JSONB, nullable=False)
    desired_revision: int = 1
    satisfied_revision: int = 0
    requested_after: datetime = Field(sa_type=AwareDateTime)
    deadline: datetime | None = Field(default=None, sa_type=AwareDateTime)
    oldest_pending_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    source: str = Field(max_length=64)


class SyncSchedule(SQLModel, table=True):
    __tablename__ = "sync_schedule"
    __table_args__ = (
        ForeignKeyConstraint(
            ["scope_id", "tenant_id"],
            ["sync_scope.id", "sync_scope.tenant_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "work_class IN ('realtime','history','directory','maintenance')",
            name="ck_sync_schedule_class",
        ),
        CheckConstraint(
            "generation >= 0 AND cost > 0", name="ck_sync_schedule_numbers"
        ),
        Index(
            "ix_sync_schedule_leased",
            "leased_until",
            "lease_execution_id",
            postgresql_where=text("lease_token IS NOT NULL"),
        ),
        Index(
            "ix_sync_schedule_due",
            "physical_endpoint",
            "next_eligible_at",
            "tenant_id",
            postgresql_where=text("active IS TRUE"),
        ),
    )
    scope_id: UUID = Field(primary_key=True)
    work_class: str = Field(primary_key=True, max_length=32)
    tenant_id: UUID
    active: bool = True
    next_eligible_at: datetime = Field(sa_type=AwareDateTime)
    wait_reason: str | None = Field(default=None, max_length=32)
    physical_endpoint: str = Field(max_length=256)
    cost: float = 1
    deficit: float = 0
    generation: int = 0
    lease_token: UUID | None = None
    lease_execution_id: UUID | None = None
    lease_execution_generation: int | None = None
    leased_until: datetime | None = Field(default=None, sa_type=AwareDateTime)
    oldest_pending_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    last_dispatched_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class SyncExecutionScope(SQLModel, table=True):
    __tablename__ = "sync_execution_scope"
    __table_args__ = (
        ForeignKeyConstraint(
            ["requirement_id", "scope_id", "tenant_id"],
            [
                "sync_requirement.id",
                "sync_requirement.scope_id",
                "sync_requirement.tenant_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["report_job_id"], ["report_sync_job.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["directory_run_id"], ["ad_directory_run.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["balance_run_id"], ["report_balance_run.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "num_nonnulls(report_job_id, directory_run_id, balance_run_id) = 1",
            name="ck_sync_execution_reference",
        ),
        CheckConstraint(
            "frozen_revision > 0 AND owner_epoch > 0 AND execution_generation >= 0",
            name="ck_sync_execution_fence",
        ),
        CheckConstraint(
            "status IN ('PENDING','PUBLISHED','SUPERSEDED')",
            name="ck_sync_execution_status",
        ),
        Index(
            "ix_sync_execution_scope_pending",
            "scope_id",
            "requirement_id",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_sync_execution_requirement", "requirement_id"),
        *(
            Index(
                f"uq_sync_execution_{column}",
                column,
                "requirement_id",
                unique=True,
                postgresql_where=text(f"{column} IS NOT NULL"),
            )
            for column in ("report_job_id", "directory_run_id", "balance_run_id")
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    scope_id: UUID
    requirement_id: UUID
    report_job_id: UUID | None = None
    directory_run_id: UUID | None = None
    balance_run_id: UUID | None = None
    frozen_revision: int
    frozen_requested_after: datetime = Field(sa_type=AwareDateTime)
    owner_epoch: int
    execution_generation: int = 0
    observed_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    published_version: int | None = None
    status: str = Field(default="PENDING", max_length=16)


class SyncCostLedger(SQLModel, table=True):
    """执行代数×物理接口的成本收据；平台quota仍只有原有一份账本。"""

    __tablename__ = "sync_cost_ledger"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["report_job_id"], ["report_sync_job.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["directory_run_id"], ["ad_directory_run.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["balance_run_id"], ["report_balance_run.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "num_nonnulls(report_job_id, directory_run_id, balance_run_id) = 1",
            name="ck_sync_cost_execution",
        ),
        CheckConstraint(
            "generation > 0 AND reserved_units >= 0 AND observed_units >= 0 AND charged_units >= 0 "
            "AND receipt_version >= 0 AND settled_version >= 0 AND settled_version <= receipt_version",
            name="ck_sync_cost_numbers",
        ),
        CheckConstraint(
            "work_class IN ('realtime','history','directory','maintenance') "
            "AND jsonb_typeof(eligible_weights)='object' "
            "AND jsonb_typeof(endpoint_weights)='object' "
            "AND jsonb_typeof(producers)='object'",
            name="ck_sync_cost_contract",
        ),
        *(
            Index(
                f"uq_sync_cost_{column}",
                column,
                "generation",
                "physical_endpoint",
                unique=True,
                postgresql_where=text(f"{column} IS NOT NULL"),
            )
            for column in ("report_job_id", "directory_run_id", "balance_run_id")
        ),
        Index(
            "ix_sync_cost_unsettled",
            "tenant_id",
            "physical_endpoint",
            "id",
            postgresql_where=text("receipt_version > settled_version"),
        ),
        Index(
            "ix_sync_cost_open",
            "tenant_id",
            "physical_endpoint",
            "work_class",
            "lease_until",
            postgresql_where=text("closed IS FALSE"),
        ),
        Index("ix_sync_cost_retention", "updated_at", "id"),
        Index(
            "ix_sync_cost_abandonable",
            "lease_until",
            "id",
            postgresql_where=text("closed IS FALSE AND abandoned_at IS NULL"),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    report_job_id: UUID | None = None
    directory_run_id: UUID | None = None
    balance_run_id: UUID | None = None
    generation: int
    physical_endpoint: str = Field(max_length=256)
    work_class: str = Field(max_length=32)
    eligible_weights: dict[str, int] = Field(sa_type=JSONB, nullable=False)
    # 一次链接执行可跨多个SDK接口；冻结各接口当时的可竞争类，不把补充调用
    # 错扣到主报表接口。发送端只读这一快照，不反向获取租户cursor锁。
    endpoint_weights: dict[str, dict[str, int]] = Field(sa_type=JSONB, nullable=False)
    reserved_units: int = 0
    observed_units: int = 0
    charged_units: int = 0
    # 同代重复消费者使用各自producer累计max；总增量既不重复结算也不漏真实发送。
    producers: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    closed: bool = False
    # 已失效执行的未知尾部保守封存；不把未知发送伪装成完整收据。
    abandoned_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    receipt_version: int = 0
    settled_version: int = 0
    lease_until: datetime = Field(sa_type=AwareDateTime)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )


class SyncWorkerPool(SQLModel, table=True):
    """共享Worker池的调度游标；执行租约仍使用原typed运行/调度头。"""

    __tablename__ = "sync_worker_pool"
    __table_args__ = (
        CheckConstraint(
            "capacity > 0 AND prefetch_slots >= 0 AND pool_name='background'",
            name="ck_sync_worker_pool_contract",
        ),
    )
    pool_name: str = Field(primary_key=True, max_length=32)
    capacity: int = 4
    prefetch_slots: int = 0
    fairness_state: dict[str, float] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    readiness_cursor: dict[str, str] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )


class SyncArchiveFinalization(SQLModel, table=True):
    """删源与持久归档完成标记之间的短期恢复意图；标记验证成功后删除。"""

    __tablename__ = "sync_archive_finalization"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        CheckConstraint(
            "kind IN ('report','directory','balance') AND byte_length>0 "
            "AND digest ~ '^[a-f0-9]{64}$'",
            name="ck_sync_archive_finalization_contract",
        ),
        Index(
            "ix_sync_archive_finalize_due",
            "created_at",
            "tenant_id",
            "kind",
            "execution_id",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    kind: str = Field(primary_key=True, max_length=16)
    execution_id: UUID = Field(primary_key=True)
    digest: str = Field(max_length=64)
    object_key: str = Field(max_length=1024)
    byte_length: int = Field(sa_type=BigInteger)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
