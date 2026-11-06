"""预览兼容性和实际发送共用的冻结父对象/CTA请求编译；不访问数据库或平台。"""

from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from typing import Any, Literal

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import CREATE_HIGHEST_VALUE_EVENT
from app.modules.builds.preview_schemas import FrozenUnit
from app.modules.builds.request_compiler import compile_request, cta_portfolio


def exact_number(value: Decimal) -> int | float:
    """The official JSON serializer needs numbers; reject any lossy conversion."""
    if not value.is_finite():
        raise DomainError("invalid_build_request", "预算或出价无效")
    number = float(value)
    if not isfinite(number) or Decimal(str(number)) != value:
        raise DomainError("decimal_serialization_loss", "数值精度超出官方 SDK 支持范围")
    return int(value) if value == value.to_integral() else number


def parent_request(
    frozen: FrozenUnit,
    *,
    kind: Literal["CAMPAIGN", "ADGROUP"],
    name: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    snapshot = frozen.scene_snapshot
    fixed: dict[str, Any] = {"advertiser_id": frozen.advertiser_id}
    resolved: dict[str, Any]
    if kind == "CAMPAIGN":
        fixed.update(
            campaign_name=name,
            budget_strategy=frozen.budget_strategy,
            creation_status=frozen.creation_status,
        )
        if frozen.budget_strategy == "SERIES":
            fixed["budget"] = exact_number(frozen.budget)
        resolved = snapshot["campaign_fields"]
    elif kind == "ADGROUP":
        fixed.update(
            campaign_id=parent_id,
            adgroup_name=name,
            budget_strategy=frozen.budget_strategy,
            bid_strategy=frozen.bid_strategy,
            creation_status=frozen.creation_status,
        )
        if frozen.budget_strategy == "ADGROUP":
            fixed["budget"] = exact_number(frozen.budget)
        if frozen.bid_strategy == "TARGET_ROAS":
            if frozen.target_roas is None:
                raise DomainError("bid_strategy_invalid", "目标 ROAS 缺少冻结目标值")
            fixed["roas_bid"] = exact_number(frozen.target_roas)
        resolved = dict(snapshot["adgroup_fields"])
        if frozen.bid_strategy == "HIGHEST_VALUE":
            resolved.update(
                optimization_goal="VALUE",
                optimization_event=CREATE_HIGHEST_VALUE_EVENT,
                deep_bid_type="VO_HIGHEST_VALUE",
            )
        else:
            resolved["deep_bid_type"] = "VO_MIN_ROAS"
        if not resolved.get("targeting_spec", {}).get("location_ids"):
            raise DomainError("scene_targeting_unavailable", "缺少已核实的投放地区")
        # 立即开始在首次执行时生成 UTC 时间；指定区间沿用预览冻结值。
        if frozen.schedule_type == "SCHEDULE_FROM_NOW":
            resolved.update(
                schedule_type="SCHEDULE_FROM_NOW",
                # 官方接口用 SCHEDULE_FROM_NOW 同时覆盖“立即开始”和“指定开始时间”；
                # 没有指定时间时才在首次执行边界生成当前 UTC 时间。
                schedule_start_time=frozen.schedule_start_time
                or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            )
        else:
            resolved.update(
                schedule_type="SCHEDULE_START_END",
                schedule_start_time=frozen.schedule_start_time,
                schedule_end_time=frozen.schedule_end_time,
            )
    return compile_request(kind.lower(), fixed=fixed, resolved=resolved)


def cta_request(frozen: FrozenUnit, selected: set[str]) -> dict[str, Any]:
    content = []
    seen: set[str] = set()
    for asset in frozen.scene_snapshot["cta_fields"].get("recommend_assets", []):
        ids = [value for value in asset["asset_ids"] if value in selected]
        if ids:
            content.append({"asset_ids": ids, "asset_content": asset["asset_content"]})
            seen.update(ids)
    if seen != selected:
        raise DomainError("cta_unavailable", "所选 CTA 缺少冻结推荐证据")
    return cta_portfolio(advertiser_id=frozen.advertiser_id, assets=content)
