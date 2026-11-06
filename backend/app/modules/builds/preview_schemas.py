from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.builds.route_schemas import ExecutionRoutePublic

from .creative_units import FrozenCreativeUnit
from .targeting_schemas import AudienceTargeting

Readiness = Literal["READY", "PREPARING", "BLOCKED"]


def budget_strategy_label(strategy: str | None) -> str:
    """把冻结合同中的预算枚举转换为业务页面使用的中文名称。"""
    return "组预算合计" if strategy == "ADGROUP" else "系列预算合计"


def budget_unit_label(strategy: str | None) -> str:
    return "组日预算" if strategy == "ADGROUP" else "系列日预算"


def bid_strategy_label(strategy: str | None) -> str:
    return "目标 ROAS" if strategy == "TARGET_ROAS" else "最高价值"


def generation_mode_label(mode: str | None) -> str:
    return "按素材数量" if mode == "BY_MATERIAL" else "固定数量"


def build_structure_summary(
    *,
    campaign_count: int,
    group_count: int,
    ad_count: int,
    creative_count: int,
    group_generation_mode: str | None,
    ad_generation_mode: str | None,
    material_allocation_count: int,
    unique_material_count: int,
) -> str:
    """生成预览和提交页共用的业务摘要，不暴露内部 base_ad_no。"""
    per_group = (
        f"每组 {ad_count // group_count} 个广告"
        if group_count and ad_count % group_count == 0
        else "各组广告数量按策略生成"
    )
    per_ad = (
        f"每个广告 {material_allocation_count // ad_count} 个素材"
        if ad_count and material_allocation_count % ad_count == 0
        else "每个广告素材数量按策略生成"
    )
    return (
        f"{campaign_count} 个系列、{group_count} 个广告组、{per_group}、"
        f"每基础广告文案版本数 K={creative_count}、最终 {ad_count} 个广告、{per_ad}；"
        f"广告组{generation_mode_label(group_generation_mode)}，广告{generation_mode_label(ad_generation_mode)}；"
        f"去重素材 {unique_material_count} 个，分配引用 {material_allocation_count} 次"
    )


def frozen_bid_strategy(
    *,
    scene_snapshot: dict[str, Any],
    preview_config: dict[str, Any],
    target_roas: Decimal | None,
) -> Literal["HIGHEST_VALUE", "TARGET_ROAS"]:
    """Project old previews without inventing a new highest-value bid.

    Older frozen previews predate ``bid_strategy`` but stored a non-null
    ``target_roas``. Preserve that target-ROAS intent when reconstructing the
    immutable execution input; only previews with no target value default to
    highest value.
    """
    strategy = scene_snapshot.get("bid_strategy") or preview_config.get("bid_strategy")
    if strategy in {"HIGHEST_VALUE", "TARGET_ROAS"}:
        return strategy
    return "TARGET_ROAS" if target_roas is not None else "HIGHEST_VALUE"


class PreviewAccepted(BaseModel):
    preview_id: UUID


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)
    refresh_continuation: bool = Field(default=False, strict=True)


class PreviewGenerationProgress(BaseModel):
    phase: Literal["inputs", "dramas", "units", "digest", "complete"]
    completed_units: int
    total_units: int | None
    updated_at: datetime


class ExcludedDramaPublic(BaseModel):
    """整部剧在所有账户上都被排除（例如没有匹配到素材），本次不会搭建。"""

    drama_id: UUID
    title: str
    reason_codes: list[str] = Field(default_factory=list)


class PreviewSummary(BaseModel):
    can_refresh_continuation: bool = False
    continuation_source_ids: list[UUID] = Field(default_factory=list)
    continuation_unit_count: int = 0
    continuation_blocked_count: int = 0
    reused_campaign_count: int = 0
    reused_adgroup_count: int = 0
    reused_ad_count: int = 0
    new_campaign_count: int = 0
    new_adgroup_count: int = 0
    new_ad_count: int = 0
    contract_version: Literal[1, 2]
    media_kind: Literal["VIDEO", "CAROUSEL"]
    excluded_image_count: int = 0
    excluded_images: list[dict[str, Any]] = Field(default_factory=list)
    targeting: AudienceTargeting | None = None
    excluded_dramas: list[ExcludedDramaPublic] = Field(default_factory=list)
    targeting_region_codes: list[str] = Field(default_factory=list)
    skipped_material_count: int = 0
    generation_progress: PreviewGenerationProgress
    execution_route: ExecutionRoutePublic | None = None
    submission_id: UUID | None
    preview_id: UUID
    draft_id: UUID
    draft_revision: int
    bc_id: str
    status: Literal["BUILDING", "FROZEN", "OBSOLETE", "FAILED"]
    currency: str
    campaign_count: int
    adgroup_count: int
    ad_count: int
    blocked_count: int
    preparing_count: int
    input_issue_count: int
    total_unit_count: int
    daily_budget_sum: Decimal
    daily_budget_label: str = "系列预算合计"
    budget_strategy: Literal["SERIES", "ADGROUP"] = "SERIES"
    bid_strategy: Literal["HIGHEST_VALUE", "TARGET_ROAS"] = "HIGHEST_VALUE"
    creation_status: Literal["ENABLE", "DISABLE"] = "ENABLE"
    schedule_type: Literal["SCHEDULE_FROM_NOW", "SCHEDULE_START_END"] = (
        "SCHEDULE_FROM_NOW"
    )
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None
    group_generation_mode: Literal["FIXED", "BY_MATERIAL"] = "FIXED"
    ad_generation_mode: Literal["FIXED", "BY_MATERIAL"] = "BY_MATERIAL"
    creative_count: int = 1
    unique_material_count: int = 0
    material_allocation_count: int = 0
    structure_summary: str = ""
    content_digest: str | None
    error_code: str | None
    created_at: datetime


class PreviewUnit(BaseModel):
    skipped_material_count: int = 0
    unit_id: UUID
    drama_id: UUID
    title: str
    advertiser_id: str
    currency: str
    budget: Decimal
    campaign_name: str
    readiness: Readiness
    reason_codes: list[str]
    group_count: int
    ad_count: int
    material_count: int = 0
    unique_material_count: int = 0
    material_allocation_count: int = 0
    group_summary: str = ""
    ad_summary: str = ""
    material_summary: str = ""
    structure_summary: str = ""


class FrozenUnit(BaseModel):
    contract_version: Literal[1, 2]
    media_kind: Literal["VIDEO", "CAROUSEL"]
    targeting: AudienceTargeting | None = None
    targeting_region_codes: list[str] = Field(default_factory=list)
    model_config = ConfigDict(frozen=True)
    unit_id: UUID
    preview_id: UUID
    tenant_id: UUID
    drama_id: UUID
    link_id: UUID
    strategy_version_id: UUID
    bc_id: str
    advertiser_id: str
    connection_id: UUID
    currency: str
    timezone: str
    campaign_name: str
    protected_base: str
    url: str
    budget: Decimal
    target_roas: Decimal | None
    # 预览冻结后执行只读取这两个值，不再从可编辑策略版本回读。
    budget_strategy: Literal["SERIES", "ADGROUP"] = "SERIES"
    bid_strategy: Literal["HIGHEST_VALUE", "TARGET_ROAS"] = "HIGHEST_VALUE"
    creation_status: Literal["ENABLE", "DISABLE"] = "ENABLE"
    schedule_type: Literal["SCHEDULE_FROM_NOW", "SCHEDULE_START_END"] = (
        "SCHEDULE_FROM_NOW"
    )
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None
    readiness: Readiness
    reason_codes: tuple[str, ...]
    scene_snapshot: dict[str, Any]


class FrozenAd(BaseModel):
    included: bool = True
    reason_codes: tuple[str, ...] = ()
    creative_units: tuple[FrozenCreativeUnit, ...] = ()
    model_config = ConfigDict(frozen=True)
    ad_id: UUID
    base_ad_no: int
    creative_no: int
    name: str
    copy_id: UUID
    text: str
    cta_option_ids: tuple[str, ...]
    material_ids: tuple[UUID, ...]


class FrozenGroup(BaseModel):
    contract_version: Literal[1, 2] = 1
    material_count: int | None = None
    creative_unit_count: int | None = None
    ad_count: int | None = None
    sample_material_ids: tuple[UUID, ...] = ()
    model_config = ConfigDict(frozen=True)
    group_id: UUID
    group_no: int
    name: str
    material_ids: tuple[UUID, ...] | None
    ads: tuple[FrozenAd, ...] | None


class PreviewInputPublic(BaseModel):
    kind: str
    line_no: int
    raw_text: str
    status: str
    reason_code: str | None
    duplicate_of: int | None


class PreviewDramaPublic(BaseModel):
    skipped_material_count: int = 0
    drama_id: UUID
    title: str
    account_count: int
    ready_count: int
    preparing_count: int
    blocked_count: int
    material_count: int
    material_group_count: int
    eligible_campaign_count: int
    eligible_adgroup_count: int
    eligible_ad_count: int
    daily_budget_sum: Decimal
    unique_material_count: int = 0
    material_allocation_count: int = 0
    ad_material_allocation_count: int = 0
    daily_budget_label: str = "系列预算合计"
    structure_summary: str = ""


class SkippedMaterialPublic(BaseModel):
    material_id: UUID
    file_name: str
    reason_code: str
