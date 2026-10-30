"""查询投影持久合同；不降低原始 NUMERIC 精度，不把素材类型加入源事实身份。"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

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
