"""复制合同只改用户明确选择的字段；当前观测缺失时绝不借策略默认值补齐。"""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.core.errors import DomainError

from .request_compiler import decode_observed_intent, encode_intent

COPY_KEY = "campaign_copy"


def copy_ad_texts(snapshot: dict[str, Any], ad_id: object) -> tuple[str, ...]:
    return tuple(
        item["ad_text"]
        for item in snapshot.get(COPY_KEY, {})
        .get("ads", {})
        .get(str(ad_id), {})
        .get("ad_text_list", ())
    )


# 只允许剥离明确无创建语义的目录/传输元数据，未知业务字段必须阻断。
READ_METADATA = {
    "campaign_id",
    "adgroup_id",
    "smart_plus_ad_id",
    "ad_id",
    "request_id",
    "create_time",
    "modify_time",
    "created_time",
    "modified_time",
    "review_status",
    "secondary_status",
    "primary_status",
    "status",
    "campaign_name",
    "adgroup_name",
    "ad_name",
    "skip_learning_phase",
    "optimization_event_type",
}
REQUIRED = {
    "CAMPAIGN": {
        "advertiser_id",
        "campaign_name",
        "objective_type",
        "app_promotion_type",
        "campaign_type",
        "budget",
        "budget_mode",
        "budget_optimize_on",
        "operation_status",
    },
    "ADGROUP": {
        "advertiser_id",
        "campaign_id",
        "adgroup_name",
        "minis_id",
        "promotion_type",
        "optimization_goal",
        "optimization_event",
        "bid_type",
        "deep_bid_type",
        "billing_event",
        "placement_type",
        "placements",
        "schedule_type",
        "schedule_start_time",
        "targeting_spec",
        "targeting_optimization_mode",
        "operation_status",
    },
    "AD": {
        "advertiser_id",
        "adgroup_id",
        "ad_name",
        "operation_status",
        "creative_list",
        "ad_text_list",
        "landing_page_url_list",
        "ad_configuration",
    },
}


def _drop_default(body, key, expected):
    """只剥离已知的读取默认形态；不同值保留给严格编译报出具体字段。"""
    if key in body and type(body[key]) is type(expected) and body[key] == expected:
        body.pop(key)


def _normalize_observation(kind, body):
    if kind == "CAMPAIGN":
        _drop_default(body, "is_search_campaign", False)
    if kind == "ADGROUP":
        # 平台把系列预算的子组回读为0/INFINITE，创建时应省略组预算。
        if body.get("budget_mode") == "BUDGET_MODE_INFINITE" and body.get("budget") in (
            0,
            "0",
            "0.0",
        ):
            body.pop("budget", None)
            body.pop("budget_mode", None)
        for key in ("min_budget", "conversion_bid_price"):
            if body.get(key) in (0, "0", "0.0"):
                body.pop(key, None)
        if body.get("schedule_type") == "SCHEDULE_FROM_NOW":
            body.pop("schedule_end_time", None)
        target = body.get("targeting_spec")
        if isinstance(target, dict):
            ages = ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"]
            if target.get("spc_audience_age") == "OVER_TWENTY_FIVE":
                ages = ages[1:]
            if body.get("targeting_optimization_mode") == "AUTOMATIC":
                if set(target.get("age_groups") or []) == set(ages):
                    target.pop("age_groups", None)
                _drop_default(target, "gender", "GENDER_UNLIMITED")
            _drop_default(target, "languages", [])
            _drop_default(target, "age_groups", [])
            _drop_default(target, "spending_power", "ALL")
            _drop_default(target, "brand_safety_type", "NO_BRAND_SAFETY")
        for key in (
            "is_hfss",
            "is_lhf_compliance",
            "search_result_enabled",
            "organic_comment_disabled",
        ):
            _drop_default(body, key, False)
        _drop_default(body, "pacing", "PACING_MODE_SMOOTH")
    if kind == "AD":
        for key in ("deeplink_list", "tracking_pixel_id_list", "utm_params"):
            _drop_default(body, key, [])
        config = body.get("ad_configuration")
        if isinstance(config, dict):
            _drop_default(config, "catalog_creative_toggle", False)
            _drop_default(config, "fallback_type", "UNSET")
    return body


def observed_body(kind: str, configuration: dict[str, Any]) -> dict[str, Any]:
    """严格反编译当前目录；DTO 默认值不能替代显式的来源事实。"""
    if not isinstance(configuration, dict) or REQUIRED[kind] - configuration.keys():
        raise DomainError(
            "copy_source_incomplete", "来源当前配置缺少必要字段，请更新目录后重试"
        )
    body = deepcopy(configuration)
    keep = REQUIRED[kind]
    for key in READ_METADATA - keep:
        body.pop(key, None)
    body = _normalize_observation(kind, body)
    if kind == "AD":
        entries = body.get("creative_list")
        if not isinstance(entries, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("creative_info"), dict)
            for item in entries
        ):
            raise DomainError("copy_source_incomplete", "来源创意结构不完整")
        for creative in entries:
            for key in (
                "ad_material_id",
                "smart_plus_creative_id",
                "material_operation_status",
            ):
                creative.pop(key, None)
            info = creative.get("creative_info", {})
            info.pop("material_name", None)
            # TikTok 广告目录有时返回空视频文件名；创建接口用 video_id
            # 和封面 ID 识别素材，空名称应按“未提供”省略，不能阻断复制。
            video_info = info.get("video_info")
            if isinstance(video_info, dict) and video_info.get("file_name") == "":
                video_info.pop("file_name", None)
    if kind == "CAMPAIGN" and (
        body.get("budget") is None or type(body.get("budget_optimize_on")) is not bool
    ):
        raise DomainError("copy_source_incomplete", "来源系列预算或预算优化事实缺失")
    if kind == "ADGROUP":
        if (body.get("budget") is not None and body.get("budget_mode") is None) or (
            body.get("schedule_type") == "SCHEDULE_START_END"
            and not body.get("schedule_end_time")
        ):
            raise DomainError("copy_source_incomplete", "来源组预算模式或结束排期缺失")
        target = body.get("targeting_spec")
        if (
            not isinstance(target, dict)
            or not target.get("location_ids")
            or target.get("spc_audience_age")
            not in {"OVER_EIGHTEEN", "OVER_TWENTY_FIVE"}
            or body.get("targeting_optimization_mode") != "AUTOMATIC"
            or target.get("age_groups")
            or target.get("gender") not in (None, "GENDER_UNLIMITED")
        ):
            raise DomainError(
                "copy_targeting_unsupported",
                "来源当前定向不能由现有小程序创建通道无损复制",
            )
        if (
            body.get("optimization_event") == "IMPRESSION_LEVEL_AD_REVENUE"
            and body.get("vbo_window") != "ZERO_DAY"
        ):
            raise DomainError(
                "copy_source_incomplete", "来源缺少已核实的当日价值优化窗口"
            )
    try:
        return encode_intent(decode_observed_intent(kind, body))
    except (ValidationError, ValueError, TypeError) as error:
        layer = {"CAMPAIGN": "系列", "ADGROUP": "广告组", "AD": "广告"}[kind]
        fields = (
            ", ".join(
                sorted(
                    {
                        ".".join(map(str, item["loc"]))
                        for item in error.errors(include_input=False, include_url=False)
                    }
                )
            )
            if isinstance(error, ValidationError)
            else "配置值"
        )
        raise DomainError(
            "copy_source_unsupported",
            f"来源{layer}配置无法继承，请核对字段：{fields}",
        ) from error


def copy_parent_request(frozen, *, kind: str, name: str, parent_id=None, group_id=None):
    contract = frozen.scene_snapshot[COPY_KEY]
    if contract.get("version") not in {1, 2}:
        raise DomainError("copy_contract_invalid", "复制合同版本不可用")
    body = deepcopy(
        contract["campaign"]
        if kind == "CAMPAIGN"
        else contract["groups"][str(group_id)]
    )
    body["advertiser_id"] = frozen.advertiser_id
    body["campaign_name" if kind == "CAMPAIGN" else "adgroup_name"] = name
    body["operation_status"] = "ENABLE"
    if kind == "ADGROUP":
        body["campaign_id"] = parent_id
        delivery = contract["delivery_config"]
        body.pop("schedule_end_time", None)
        body["schedule_type"] = (
            "SCHEDULE_START_END"
            if delivery["schedule_mode"] == "START_END"
            else "SCHEDULE_FROM_NOW"
        )
        body["schedule_start_time"] = delivery.get(
            "schedule_start_time"
        ) or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        if delivery["schedule_mode"] == "START_END":
            body["schedule_end_time"] = delivery["schedule_end_time"]
    return encode_intent(decode_observed_intent(kind, body))


def group_setting(body: dict[str, Any], regions) -> dict[str, Any]:
    """用已核实的地区ID字典展示当前设置；历史选择和目标默认值都不参与。"""
    from .targeting_schemas import AudienceTargeting

    target = body["targeting_spec"]
    mapping = {item["location_id"]: item["region_code"] for item in regions}
    ids = target["location_ids"]
    targeting = None
    if ids and all(identity in mapping for identity in ids):
        targeting = AudienceTargeting(
            region_mode="SELECTED",
            region_codes=tuple(mapping[identity] for identity in ids),
            languages=tuple(target.get("languages") or ()),
            age_groups=("AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100")
            if target["spc_audience_age"] == "OVER_TWENTY_FIVE"
            else (),
        ).model_dump(mode="json")
    return {
        "name": body["adgroup_name"],
        "budget": body.get("budget"),
        "target_roas": body.get("roas_bid"),
        "budget_strategy": "ADGROUP" if body.get("budget") is not None else "SERIES",
        "bid_strategy": "TARGET_ROAS"
        if body.get("roas_bid") is not None
        else "HIGHEST_VALUE",
        "targeting": targeting,
        "targeting_summary": "地区ID："
        + ", ".join(ids)
        + "；语言："
        + (", ".join(target.get("languages") or ()) or "不限")
        + "；最低年龄："
        + ("25岁" if target["spc_audience_age"] == "OVER_TWENTY_FIVE" else "18岁"),
    }
