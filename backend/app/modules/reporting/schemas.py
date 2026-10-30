"""报表查询边界 DTO。

这些 DTO 只描述已发布的本地报表合同。预算模式、目标 ROAS 和创建时间来自
目录配置，D0 ROAS 来自已发布事实聚合；它们由查询层显式应用，不能伪装成
独立的经济事实列。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.integrations.tiktok.contracts.carousel import CarouselCreativeIdentity

Dimension = Literal["account", "campaign", "adgroup", "ad", "material", "drama"]
# PARTIAL 表示部分合计：只合计有值的成员（跨日、跨账户或跨对象），缺失成员既不
# 参与求和也不当成 0；会出现在报表行、汇总卡片和趋势点上（设计 7.3）。
Availability = Literal[
    "AVAILABLE", "PARTIAL", "MISSING", "UNAVAILABLE", "UNSUPPORTED", "FAILED"
]
CANONICAL_METRIC_KEYS = frozenset(
    {
        "spend",
        "impressions",
        "clicks",
        "ad_impression_events",
        "revenue_d0",
        "revenue_d3",
        "revenue_d7",
        "revenue_d14",
        "revenue_d30",
        "revenue_total",
        "roas_d0",
        "roas_d3",
        "roas_d7",
        "roas_d14",
        "roas_d30",
        "roas_total",
        "ctr",
        "cpm",
        "cpc",
        "cost_per_ad_impression_event",
    }
)


def _finite(value: Decimal | None) -> Decimal | None:
    if value is not None and not value.is_finite():
        raise ValueError("numeric thresholds must be finite")
    return value


class ReportingFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: Dimension
    start_date: date
    end_date: date
    advertiser_ids: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()
    query: str | None = None
    ad_types: tuple[str, ...] = ()
    operation_statuses: tuple[str, ...] = ()
    review_statuses: tuple[str, ...] = ()
    # 目录配置筛选由汇总查询显式应用。
    budget_modes: tuple[str, ...] = ()
    created_from: date | None = None
    created_to: date | None = None
    naming_status: str | None = None
    min_spend: Decimal | None = None
    max_spend: Decimal | None = None
    min_d0_roas: Decimal | None = None
    max_d0_roas: Decimal | None = None
    min_target_roas: Decimal | None = None
    max_target_roas: Decimal | None = None
    sort_by: str = "row_key"
    sort_direction: Literal["asc", "desc"] = "asc"
    # 默认只列所选日期有数据的对象；大租户目录有数万个无数据的历史对象，
    # 逐一补 0 会让首屏查询慢到几十秒。勾选后才列出目录全部对象（账户维度始终全部列出）。
    include_empty: bool = False

    @field_validator(
        "min_spend",
        "max_spend",
        "min_d0_roas",
        "max_d0_roas",
        "min_target_roas",
        "max_target_roas",
        mode="before",
    )
    @classmethod
    def finite_threshold(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return _finite(Decimal(str(value)))
        except Exception as exc:
            raise ValueError("numeric thresholds must be finite decimals") from exc

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @model_validator(mode="after")
    def validate_ranges(self) -> ReportingFilter:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if (
            self.created_from
            and self.created_to
            and self.created_from > self.created_to
        ):
            raise ValueError("created_from must be on or before created_to")
        for lower, upper in (
            (self.min_spend, self.max_spend),
            (self.min_d0_roas, self.max_d0_roas),
            (self.min_target_roas, self.max_target_roas),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("minimum threshold must not exceed maximum threshold")
        return self


class MetricVector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str
    timezone: str
    attribution: str
    # A 当前没有 optimization_goal 坐标；None 是唯一可发布值。
    optimization_goal: str | None = None
    values: dict[str, Decimal | None] = Field(default_factory=dict)
    availability: dict[str, Availability] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_published_metrics(self) -> MetricVector:
        if self.optimization_goal is not None:
            raise ValueError("optimization_goal is not a published report coordinate")
        unknown = (set(self.values) | set(self.availability)) - (CANONICAL_METRIC_KEYS)
        if unknown:
            raise ValueError(f"unsupported report metric keys: {sorted(unknown)}")
        if set(self.values) != set(self.availability):
            raise ValueError("metric values and availability must have the same keys")
        return self


class ReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_key: str
    # 描述性轮播组合；指标仍以广告行唯一键发布。
    carousel_creatives: tuple[CarouselCreativeIdentity, ...] = ()
    display: dict[str, str | None] = Field(default_factory=dict)
    refs: tuple[EntityRef, ...] = ()
    material_uses: tuple[MaterialUseRef, ...] = ()
    metric_buckets: tuple[MetricVector, ...] = ()
    capabilities: dict[str, bool] = Field(default_factory=dict)
    directory_versions: dict[str, int] = Field(default_factory=dict)
    membership_digest: str = ""
    coverage: dict[str, object] = Field(default_factory=dict)


class QuerySnapshotPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    snapshot_at: datetime
    expires_at: datetime
    filters: ReportingFilter
    publication_versions: dict[str, int] = Field(default_factory=dict)


class AdsQueryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: QuerySnapshotPublic
    items: tuple[ReportRow, ...] = ()
    total: int = 0
    summary: dict[str, object] = Field(default_factory=dict)
    coverage: dict[str, object] = Field(default_factory=dict)
    next_cursor: str | None = None
    metrics_observed_at: datetime | None = None
    ordering_basis: Literal["snapshot"] = "snapshot"
    metrics_coverage: dict[str, object] = Field(default_factory=dict)


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    mode: Literal["EXPLICIT", "ALL_MATCHING"]
    row_keys: tuple[str, ...] = ()
    excluded_row_keys: tuple[str, ...] = ()


class AdMaterialSelectionRequest(BaseModel):
    """广告详情里只针对本广告的素材启停：按广告内素材 ID 选取。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ad_material_ids: tuple[str, ...]


class FrozenSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selection_id: UUID
    snapshot_id: UUID
    refs: tuple[EntityRef, ...] = ()
    material_uses: tuple[MaterialUseRef, ...] = ()
    membership_digest: str
    expires_at: datetime


class SavedViewPublic(BaseModel):
    """当前操作者在一个租户/BC 下保存的私有筛选视图。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    filters: ReportingFilter
    columns: tuple[str, ...] = ()
    created_at: datetime


class SavedViewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    filters: ReportingFilter
    columns: tuple[str, ...] = ()


class SavedViewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    filters: ReportingFilter | None = None
    columns: tuple[str, ...] | None = None


class ExportPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    status: Literal["QUEUED", "RUNNING", "COMPLETE", "FAILED", "EXPIRED"]
    coverage: dict[str, object] = Field(default_factory=dict)
    expires_at: datetime


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128)


class SyncRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    advertiser_ids: tuple[str, ...] = ()

    @field_validator("advertiser_ids")
    @classmethod
    def valid_accounts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not v.strip() or len(v) > 128 for v in values
        ):
            raise ValueError("advertiser_ids must be unique nonempty identifiers")
        return values


class SyncRefreshPublic(BaseModel):
    queued: int = 0
    accounts: int = 0
    status: Literal["QUEUED", "SYNC_DISABLED", "API_BINDING_REQUIRED"]


class SyncAccountStatus(BaseModel):
    advertiser_id: str
    timezone: str
    local_today: date | None = None
    last_success_at: datetime | None = None
    incomplete_dates: tuple[date, ...] = ()
    failed_dates: tuple[date, ...] = ()


class SyncPlanStatus(BaseModel):
    slowed: bool | None = None
    enabled: bool
    disabled_reason: str | None = None
    next_hot_at: datetime | None = None


class SyncHealthPublic(BaseModel):
    status: Literal["HEALTHY", "DEGRADED", "DISABLED"]
    observed_at: datetime | None = None
    coverage_observed_at: datetime | None = None
    coverage_completed_at: datetime | None = None
    coverage_status: Literal["PENDING", "COMPLETE", "FAILED"] | None = None
    stale_ad_accounts: int = 0
    stale_material_accounts: int = 0
    missing_directory_ads: int = 0
    unlinked_material_posts: int = 0
    pending_actions: int = 0
    oldest_data_at: datetime | None = None
    issues: tuple[str, ...] = ()


class SyncStatusPublic(BaseModel):
    publication_version: str
    accounts: tuple[SyncAccountStatus, ...]
    # 完整数据起始日：此日至账户本地昨天全部账户已同步完成；无完整区间时为空。
    complete_from: date | None = None
    plan: SyncPlanStatus
    health: SyncHealthPublic | None = None


class MetricDefinitionPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    unit: Literal["money", "count", "ratio", "multiple"]
    additive: bool
    basic_field: str | None
    material_field: str | None
    basic_name_zh: str
    material_name_zh: str | None
    basic_name_en: str
    material_name_en: str | None
    basic_label: str
    material_label: str | None


TrendLevel = Literal["account", "campaign", "adgroup", "ad", "material"]


class TrendPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 日粒度为账户本地日 YYYY-MM-DD；小时粒度为 UTC 整点 ISO 字符串（分钟精度）。
    at: str
    buckets: tuple[MetricVector, ...] = ()
    coverage: Literal["COMPLETE", "INCOMPLETE", "MISSING"] = "MISSING"


class TrendPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: TrendLevel
    granularity: Literal["day", "hour"]
    # 所有事实同一时区时为该时区；多个时区为 MIXED；无数据为 UTC。
    timezone: str
    points: tuple[TrendPoint, ...] = ()
    # 小时趋势按需拉取：仍有缺失或过期的小时数据在排队/拉取中，页面需轮询。
    syncing: bool = False
