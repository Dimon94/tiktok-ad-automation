# backend/app/modules/reporting/metrics.py
"""平台报表字段 → 存储列。

基础报表 dN = 点击后 N+1 天累计，因此 D3/D7/D14/D30 取 d2/d6/d13/d29；
创意与广告使用同一套基础报表字段，事实粒度分别保存。
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.core.errors import DomainError
from app.modules.reporting.metric_catalog import NATIVE_CONTRACT, basic_fields

_BASIC = basic_fields()
COLUMNS = tuple(_BASIC)

# 广告报表随行带回上级 ID、名称和类型，新建对象无需等待目录同步即可归组。
AD_DAY_METRICS = (
    *_BASIC.values(),
    "campaign_id",
    "adgroup_id",
    "campaign_name",
    "adgroup_name",
    "ad_name",
    "campaign_automation_type",
)
CAMPAIGN_METRICS = (
    *_BASIC.values(),
    "native_growth_ad_revenue_roas",
    "native_growth_ad_revenue_roas_d0",
    "campaign_name",
)
# 小时报只取适用的成本/计数，日点击归因收入不能混入小时桶。
CAMPAIGN_HOUR_METRICS = ("spend", "impressions", "clicks", "campaign_name")
PLATFORM_RATIOS = {
    "platform_roas_total": "native_growth_ad_revenue_roas",
    "platform_roas_d0": "native_growth_ad_revenue_roas_d0",
}


def metric_evidence(metrics: dict[str, Any], *, level: str = "ad") -> dict[str, Any]:
    # 保存请求合同和逐字段状态，不保存响应行、令牌或客户素材。
    # 广告日报未请求平台比率，不能把它解释为平台漏返字段。
    from app.modules.reporting.levels import LEVELS

    contract = LEVELS[f"{level}_day"]
    requested_ratios = {
        k: v for k, v in PLATFORM_RATIOS.items() if v in contract.metrics
    }
    fields = _BASIC | requested_ratios
    evidence: dict[str, dict[str, Any]] = {}
    for key, field in fields.items():
        raw = metrics.get(field)
        value = parse_decimal(raw)
        evidence[key] = {
            "field": field,
            "status": "ABSENT"
            if field not in metrics
            else "NULL"
            if raw is None
            else "DASH"
            if raw == "-"
            else "VALUE",
        }
        # 金额只存事实 Numeric 列；仅非可加的平台比率作为独立观测保留。
        if key in requested_ratios:
            evidence[key]["value"] = str(value) if value is not None else None
    return {
        "contract_revision": NATIVE_CONTRACT,
        "level": level,
        "field_states": {key: item["status"] for key, item in evidence.items()},
        "platform_roas": {key: evidence[key]["value"] for key in requested_ratios},
        "source_kind": "PLATFORM_DIRECT",
        "request": {
            "report_type": "BASIC",
            "service_type": "AUCTION",
            "data_level": contract.data_level,
            "dimensions": [
                contract.dimension,
                "stat_time_day",
            ],
        },
        "traffic_scope": "UNCONFIRMED",
        "d0_window": "CLICK_24H",
        "total_maturity": "UNKNOWN",
        "fields": evidence,
    }


ACCOUNT_METRICS = ("spend", "native_growth_total_ad_impression_value")


def parse_decimal(value: object) -> Decimal | None:
    if value is None or value == "-":
        return None
    if type(value) not in (str, int):
        raise DomainError("report_value_invalid", "报表指标必须是定点数字文本")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError("report_value_invalid", "报表指标不是有效定点数") from exc
    if not amount.is_finite():
        raise DomainError("report_value_invalid", "报表指标不能是无穷值")
    return amount


def basic_metric_values(metrics: dict[str, object]) -> dict[str, Decimal | None]:
    return {
        column: parse_decimal(metrics.get(field)) for column, field in _BASIC.items()
    }


def parse_local_day(raw: object) -> date:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as exc:
        raise DomainError("report_row_invalid", "报表日期维度无效") from exc


def parse_local_hour(raw: object, timezone: str) -> datetime:
    try:
        local = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        return local.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    except (ValueError, KeyError) as exc:
        raise DomainError("report_row_invalid", "报表小时维度无效") from exc


def creative_metric_evidence(metrics: dict[str, Any]) -> dict[str, Any]:
    """creative只记录实际请求与字段观察，不继承父Ad的口径确认。"""
    from app.modules.reporting.metric_catalog import CREATIVE_OBSERVED_CONTRACT

    states = {
        key: "ABSENT"
        if field not in metrics
        else "NULL"
        if metrics[field] is None
        else "DASH"
        if metrics[field] == "-"
        else "VALUE"
        for key, field in _BASIC.items()
    }
    return {
        "contract_revision": CREATIVE_OBSERVED_CONTRACT,
        "field_states": states,
        "request": {
            "report_type": "BASIC",
            "service_type": "AUCTION",
            "data_level": "AUCTION_AD",
            "dimensions": ["ad_id", "stat_time_day"],
            "metrics": list(AD_DAY_METRICS),
        },
        "traffic_scope": "UNCONFIRMED",
        "d0_window": "UNCONFIRMED",
        "total_maturity": "UNKNOWN",
    }
