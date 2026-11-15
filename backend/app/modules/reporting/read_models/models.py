"""查询投影持久合同；不降低原始 NUMERIC 精度，不把素材类型加入源事实身份。"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime
from app.modules.reporting.store_models import _Metrics


def account_reference() -> ForeignKeyConstraint:
    # 这些表完全由canonical重建；删除账户时不能因源DELETE重建的dirty元数据
    # 阻止合法清理。只有派生数据级联，源事实的外键保护不变。
    return ForeignKeyConstraint(
        ["tenant_id", "advertiser_id"],
        ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ondelete="CASCADE",
    )


class ReadBucketState(SQLModel, table=True):
    __tablename__ = "read_bucket_state"
    __table_args__ = (
        account_reference(),
        CheckConstraint(
            "source_revision >= derived_revision AND derived_revision >= 0",
            name="ck_read_bucket_versions",
        ),
        Index(
            "ix_read_bucket_complete_period",
            "tenant_id",
            "kind",
            "bucket",
            "advertiser_id",
            postgresql_where=text("derived_revision>0 AND derived_status='COMPLETE'"),
        ),
        Index(
            "ix_read_bucket_projection_period",
            "tenant_id",
            "kind",
            "bucket",
            "advertiser_id",
            postgresql_include=[
                "source_revision",
                "derived_revision",
                "derived_observed_at",
                "derived_status",
            ],
        ),
        Index(
            "ix_creative_material_dirty",
            "observed_at",
            "tenant_id",
            "advertiser_id",
            "bucket",
            postgresql_where=text(
                "source_revision > derived_revision AND kind='creative_material'"
            ),
        ),
        Index(
            "ix_read_bucket_active_dirty",
            text("bucket DESC"),
            "kind",
            "observed_at",
            "tenant_id",
            "advertiser_id",
            postgresql_where=text(
                "source_revision > derived_revision AND kind IN ('ad_day','creative_material')"
            ),
        ),
        Index(
            "ix_read_bucket_dirty",
            "observed_at",
            "tenant_id",
            "advertiser_id",
            "kind",
            "bucket",
            postgresql_where=text(
                "source_revision > derived_revision AND kind IN ('ad_day','material_day')"
            ),
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    kind: str = Field(primary_key=True, max_length=32)
    bucket: date = Field(primary_key=True)
    source_revision: int = Field(default=0, sa_type=BigInteger)
    derived_revision: int = Field(default=0, sa_type=BigInteger)
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    derived_observed_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    derived_status: str | None = Field(default=None, max_length=16)


class AdEntityRead(SQLModel, table=True):
    __tablename__ = "ad_entity_read"
    __table_args__ = (
        account_reference(),
        Index(
            "ix_ad_entity_read_parent",
            "tenant_id",
            "advertiser_id",
            "parent_kind",
            "parent_remote_id",
            "kind",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    kind: str = Field(primary_key=True, max_length=16)
    remote_id: str = Field(primary_key=True, max_length=128)
    parent_kind: str | None = Field(default=None, max_length=16)
    parent_remote_id: str | None = Field(default=None, max_length=128)
    name: str = ""
    ad_type: str = Field(max_length=64)
    operation_status: str | None = None
    review_status: str | None = None
    delivery_status: str | None = None
    budget_mode: str | None = None
    optimization_goal: str | None = None
    budget_value: Decimal | None = Field(default=None, sa_type=Numeric)
    target_roas: Decimal | None = Field(default=None, sa_type=Numeric)
    # 筛选只认 roas_bid，展示则按真值回退 target_roas，不能合并成一个字段。
    roas_bid: Decimal | None = Field(default=None, sa_type=Numeric)
    created_local_date: date | None = None
    observed_at: datetime = Field(sa_type=AwareDateTime)
    published_version: int = Field(sa_type=BigInteger)


class CampaignNameCurrent(SQLModel, table=True):
    __tablename__ = "campaign_name_current"
    __table_args__ = (account_reference(),)
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    campaign_remote_id: str = Field(primary_key=True, max_length=128)
    raw_name: str
    provider_label: str | None = None
    drama_name: str | None = None
    status: str = Field(max_length=16)
    parser_revision: int
    name_revision: int
    grouping_revision: int
    observed_at: datetime = Field(sa_type=AwareDateTime)


class AccountMembershipVersion(SQLModel, table=True):
    """不可变成员证据；退休后仍由存活快照的外键保护。"""

    __tablename__ = "account_membership_version"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        UniqueConstraint(
            "tenant_id", "advertiser_id", "id", name="uq_account_membership_scope"
        ),
        Index(
            "ix_account_membership_retired",
            "retired_at",
            postgresql_where=text("retired_at IS NOT NULL"),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    refs: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    naming_versions: dict[str, int] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    retired_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class AccountMembershipCurrent(SQLModel, table=True):
    __tablename__ = "account_membership_current"
    __table_args__ = (
        account_reference(),
        Index("ix_account_membership_current_version", "version_id"),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id", "version_id"],
            [
                "account_membership_version.tenant_id",
                "account_membership_version.advertiser_id",
                "account_membership_version.id",
            ],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    version_id: UUID


class CurrentAdMaterial(SQLModel, table=True):
    __tablename__ = "current_ad_material"
    __table_args__ = (
        account_reference(),
        UniqueConstraint(
            "tenant_id",
            "advertiser_id",
            "ad_remote_id",
            "platform_material_id",
            "ad_material_id",
            "material_type",
            name="uq_current_ad_material_usage",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_current_ad_material_video",
            "tenant_id",
            "platform_material_id",
            "advertiser_id",
        ),
        Index(
            "ix_current_ad_material_ad", "tenant_id", "advertiser_id", "ad_remote_id"
        ),
    )
    # 保留全部同版本精确身份，消费者才能证明歧义；不得用一个任取的ID掩盖冲突。
    id: UUID = Field(primary_key=True)
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    ad_remote_id: str = Field(max_length=128)
    platform_material_id: str = Field(max_length=128)
    ad_material_id: str | None = Field(default=None, max_length=128)
    material_type: str = Field(max_length=64)
    name: str = ""
    operation_status: str | None = None
    complete: bool = True
    published_version: int = Field(sa_type=BigInteger)


class _PartialMetrics(_Metrics):
    spend_has_null: bool = False
    impressions_has_null: bool = False
    clicks_has_null: bool = False
    ad_impression_events_has_null: bool = False
    revenue_d0_has_null: bool = False
    revenue_d3_has_null: bool = False
    revenue_d7_has_null: bool = False
    revenue_d14_has_null: bool = False
    revenue_d30_has_null: bool = False
    revenue_total_has_null: bool = False


class _ReadDay(_PartialMetrics):
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    stat_date: date = Field(primary_key=True)
    currency: str = Field(primary_key=True, max_length=16)
    timezone: str = Field(primary_key=True, max_length=64)
    fetched_at: datetime = Field(sa_type=AwareDateTime)
    source_revision: int = Field(sa_type=BigInteger)


class AdgroupReportDay(_ReadDay, table=True):
    __tablename__ = "adgroup_report_day"
    __table_args__ = (
        account_reference(),
        Index(
            "ix_adgroup_report_day_period", "tenant_id", "advertiser_id", "stat_date"
        ),
    )
    adgroup_id: str = Field(primary_key=True, max_length=64)
    campaign_id: str | None = Field(default=None, max_length=64)
    campaign_name: str | None = None
    adgroup_name: str | None = None


class AccountReportDay(_ReadDay, table=True):
    __tablename__ = "account_report_day"
    __table_args__ = (
        account_reference(),
        Index(
            "ix_account_report_day_period", "tenant_id", "stat_date", "advertiser_id"
        ),
    )


class AccountReportDayState(SQLModel, table=True):
    __tablename__ = "account_report_day_state"
    __table_args__ = (
        account_reference(),
        Index(
            "ix_account_report_state_period", "tenant_id", "stat_date", "advertiser_id"
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    stat_date: date = Field(primary_key=True)
    revision: int = Field(default=0, sa_type=BigInteger)
    status: str | None = Field(default=None, max_length=16)
    observed_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    last_success_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class MaterialEntityDay(_ReadDay, table=True):
    __tablename__ = "material_entity_day"
    __table_args__ = (
        account_reference(),
        Index(
            "ix_material_entity_day_page",
            "tenant_id",
            "row_key",
            "advertiser_id",
            "stat_date",
        ),
        Index(
            "ix_material_entity_day_period", "tenant_id", "advertiser_id", "stat_date"
        ),
    )
    row_key: str = Field(primary_key=True, max_length=512)
    main_material_type: str = Field(primary_key=True, max_length=64)
    main_material_name: str | None = None
    # 仅身份证据，不含原始报表正文；用于精确帖子搜索与展示，归属和金额同桶提交。
    post_ids: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(Text), nullable=False)
    )
    # 名称回退先取每个帖子在日期范围内的最近非空名，再合并到视频；不能在
    # 日聚合时提前丢掉其他帖子的名字，否则跨日名称筛选/排序会漏行。
    post_names: dict[str, str] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )


# 先定义共享日指标基类，再注册分离的读模型；不依赖app.models的导入顺序。
from app.modules.reporting.read_models.entity_metrics import (  # noqa: E402
    EntityMetricDay,  # noqa: F401
)
from app.modules.reporting.read_models.evidence import (  # noqa: E402,F401
    QuerySnapshotEvidence,
    QuerySnapshotNameVersion,
    ReportEvidenceVersion,
    ReportNameScopeCurrent,
)
from app.modules.reporting.read_models.material_memberships import (  # noqa: E402,F401
    MaterialAccountMemberCurrent,
    MaterialAdMemberCurrent,
)


class CreativeMaterialEntityDay(_ReadDay, table=True):
    """独立创意素材投影：每个经济事实只一行，筛选在汇总之前进行。"""

    __tablename__ = "creative_material_entity_day"
    evidence: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    __table_args__ = (
        Index("ix_creative_material_period", "tenant_id", "stat_date", "advertiser_id"),
        account_reference(),
        Index(
            "ix_creative_material_page",
            "tenant_id",
            "row_key",
            "advertiser_id",
            "stat_date",
        ),
        Index(
            "ix_creative_material_group",
            "tenant_id",
            "advertiser_id",
            "adgroup_id",
            "stat_date",
        ),
        Index(
            "ix_creative_material_ad",
            "tenant_id",
            "advertiser_id",
            "ad_remote_id",
            "stat_date",
        ),
    )
    fact_key: str = Field(primary_key=True, max_length=512)
    row_key: str = Field(max_length=512)
    creative_id: str | None = Field(default=None, max_length=128)
    ad_remote_id: str | None = Field(default=None, max_length=128)
    ad_material_id: str | None = Field(default=None, max_length=128)
    campaign_id: str = Field(max_length=128)
    adgroup_id: str = Field(max_length=128)
    material_type: str = Field(max_length=64)
    platform_material_id: str | None = Field(default=None, max_length=128)
    material_name: str | None = None
    assets: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    attribution_status: str = Field(max_length=32)
    source_status: str = Field(max_length=16)
    source_info: dict[str, Any] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
