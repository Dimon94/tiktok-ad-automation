"""经营版本不可变，配置修改通过范围修订围栏阻止旧任务发布。"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


def bc_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
    )


class BusinessScope(SQLModel, table=True):
    __tablename__ = "business_scope"
    __table_args__ = (bc_fk(),)
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    start_date: date
    revision: int = 1
    last_daily: date | None = None
    next_check_at: datetime = Field(default_factory=utcnow, sa_type=AwareDateTime)
    history_cursor: date | None = None
    repair_cursor: str | None = Field(default=None, max_length=512)
    published_revision: int | None = None
    published_runs: dict[str, str] = Field(default_factory=dict, sa_type=JSONB)
    initialized_at: datetime = Field(default_factory=utcnow, sa_type=AwareDateTime)


class BusinessMember(SQLModel, table=True):
    __tablename__ = "business_member"
    __table_args__ = (
        bc_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_business_member_dates",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    start_date: date
    end_date: date | None = None


class BusinessRate(SQLModel, table=True):
    __tablename__ = "business_rate"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["provider_connection.tenant_id", "provider_connection.id"],
        ),
        bc_fk(),
        CheckConstraint("rate >= 0 AND rate <= 1", name="ck_business_rate_value"),
        CheckConstraint(
            "(kind='provider' AND provider_id IS NOT NULL AND bc_id IS NULL) OR "
            "(kind='rebate' AND bc_id IS NOT NULL AND provider_id IS NULL)",
            name="ck_business_rate_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "kind",
            "scope_id",
            "revision",
            name="uq_business_rate_revision",
        ),
        Index(
            "ix_business_rate_lookup",
            "tenant_id",
            "kind",
            "scope_id",
            "effective_date",
            "revision",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    kind: str = Field(max_length=16)
    scope_id: str = Field(max_length=128)
    provider_id: UUID | None = None
    bc_id: str | None = Field(default=None, max_length=128)
    effective_date: date
    revision: int
    rate: Decimal = Field(sa_type=Numeric)
    actor_id: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow, sa_type=AwareDateTime)


class BusinessMapping(SQLModel, table=True):
    __tablename__ = "business_mapping"
    __table_args__ = (
        bc_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "drama_id"], ["provider_drama.tenant_id", "provider_drama.id"]
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    campaign_id: str = Field(primary_key=True, max_length=128)
    drama_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    evidence: str = Field(default="manual", max_length=255)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=AwareDateTime)


class BusinessEvidence(SQLModel, table=True):
    """赔付日依据不从当前出价反推历史；管理员补录须保留来源。"""

    __tablename__ = "business_evidence"
    __table_args__ = (
        bc_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    campaign_id: str = Field(primary_key=True, max_length=128)
    stat_date: date = Field(primary_key=True)
    bid: Decimal | None = Field(default=None, sa_type=Numeric)
    eligible: bool | None = None
    source: str = Field(max_length=1000)
    actor_id: UUID = Field(foreign_key="user.id")
    updated_at: datetime = Field(default_factory=utcnow, sa_type=AwareDateTime)


class BusinessRun(SQLModel, table=True):
    __tablename__ = "business_run"
    __table_args__ = (
        bc_fk(),
        UniqueConstraint("tenant_id", "bc_id", "id", name="uq_business_run_scope"),
        Index("ix_business_run_pending", "status", "created_at"),
        Index(
            "ix_business_run_scope",
            "tenant_id",
            "bc_id",
            "quarter",
            "source_signature",
            "end_date",
            "status",
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETE','SUPERSEDED','FAILED')",
            name="ck_business_run_status",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    quarter: date
    end_date: date
    scope_revision: int
    phase: str = Field(default="BASE", max_length=16)
    cursor: date
    status: str = Field(default="QUEUED", max_length=16)
    inputs: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    coverage: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    source_signature: str = Field(default="", max_length=64)
    rows_count: int = 0
    base_after: list[str] | None = Field(default=None, sa_type=JSONB)
    error: str | None = Field(default=None, max_length=255)
    created_at: datetime = Field(default_factory=utcnow, sa_type=AwareDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class BusinessPublication(SQLModel, table=True):
    __tablename__ = "business_publication"
    __table_args__ = (
        bc_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "run_id"],
            ["business_run.tenant_id", "business_run.bc_id", "business_run.id"],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    quarter: date = Field(primary_key=True)
    run_id: UUID


class BusinessAmounts(SQLModel):
    spend: Decimal | None = Field(default=None, sa_type=Numeric)
    revenue: Decimal | None = Field(default=None, sa_type=Numeric)
    provider_share: Decimal | None = Field(default=None, sa_type=Numeric)
    net_revenue: Decimal | None = Field(default=None, sa_type=Numeric)
    rebate: Decimal | None = Field(default=None, sa_type=Numeric)
    business_revenue: Decimal | None = Field(default=None, sa_type=Numeric)
    compensation: Decimal | None = Field(default=None, sa_type=Numeric)
    subsidy_comic: Decimal | None = Field(default=None, sa_type=Numeric)
    subsidy_other: Decimal | None = Field(default=None, sa_type=Numeric)
    subsidy_existing: Decimal | None = Field(default=None, sa_type=Numeric)
    issues: list[str] = Field(default_factory=list, sa_type=JSONB)


class BusinessDay(BusinessAmounts, table=True):
    __tablename__ = "business_day"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "run_id"],
            ["business_run.tenant_id", "business_run.bc_id", "business_run.id"],
        ),
        Index(
            "ix_business_day_group",
            "tenant_id",
            "bc_id",
            "run_id",
            "stat_date",
            "drama_key",
        ),
        Index(
            "ix_business_day_provider", "tenant_id", "bc_id", "provider_id", "stat_date"
        ),
        {"postgresql_partition_by": "RANGE (stat_date)"},
    )
    run_id: UUID = Field(primary_key=True)
    stat_date: date = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    campaign_id: str = Field(primary_key=True, max_length=128)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    drama_key: str = Field(max_length=255)
    drama_id: UUID | None = None
    drama_name: str
    campaign_name: str
    provider_id: UUID | None = None
    provider_name: str
    currency: str = Field(max_length=16)
    timezone: str = Field(max_length=64)
    revenue_d0: Decimal | None = Field(default=None, sa_type=Numeric)
    iaa_events: Decimal | None = Field(default=None, sa_type=Numeric)
    provider_rate: Decimal | None = Field(default=None, sa_type=Numeric)
    rebate_rate: Decimal = Field(default=Decimal(0), sa_type=Numeric)
    compensation_bid: Decimal | None = Field(default=None, sa_type=Numeric)
    compensation_eligible: bool | None = None
    compensation_source: str | None = None


class BusinessSummaryDay(BusinessAmounts, table=True):
    """按账户保留可筛选性，预先归集剧目及版权方日值。"""

    __tablename__ = "business_summary_day"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "run_id"],
            ["business_run.tenant_id", "business_run.bc_id", "business_run.id"],
        ),
        Index(
            "ix_business_summary_query",
            "tenant_id",
            "bc_id",
            "run_id",
            "kind",
            "stat_date",
        ),
    )
    run_id: UUID = Field(primary_key=True)
    stat_date: date = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    kind: str = Field(primary_key=True, max_length=16)
    drama_key: str = Field(primary_key=True, max_length=255)
    currency: str = Field(primary_key=True, max_length=16)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    drama_id: UUID | None = None
    provider_id: UUID | None = None
    drama_name: str
    provider_name: str
    campaign_name: str = ""
    campaign_id: str = ""


class BusinessExport(SQLModel, table=True):
    __tablename__ = "business_export"
    __table_args__ = (
        bc_fk(),
        UniqueConstraint("tenant_id", "bc_id", "id", name="uq_business_export_scope"),
        Index("ix_business_export_expiry", "expires_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    filters: dict[str, Any] = Field(sa_type=JSONB)
    run_ids: list[str] = Field(sa_type=JSONB)
    advertiser_ids: list[str] = Field(sa_type=JSONB)
    status: str = Field(default="QUEUED", max_length=16)
    # 文件在私有对象存储，数据库只保留定位；不向浏览器暴露存储凭据。
    object_key: str | None = None
    frozen: bool = False
    issues: list[str] = Field(default_factory=list, sa_type=JSONB)
    cursor: int = 0
    file_offset: int = 0
    rows_count: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=AwareDateTime)
    expires_at: datetime = Field(sa_type=AwareDateTime)


class BusinessExportRow(SQLModel, table=True):
    __tablename__ = "business_export_row"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "export_id"],
            [
                "business_export.tenant_id",
                "business_export.bc_id",
                "business_export.id",
            ],
        ),
        Index("ix_business_export_rounding", "export_id", "rounding_group", "row_key"),
    )
    export_id: UUID = Field(primary_key=True)
    sequence: int = Field(primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    row_key: str
    rounding_group: str
    payload: dict[str, Any] = Field(sa_type=JSONB)
    adjustments: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
