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
    fields = _BASIC | PLATFORM_RATIOS
    evidence = {}
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
            "value": str(value) if value is not None else None,
        }
    return {
        "contract_revision": NATIVE_CONTRACT,
        "level": level,
        "field_states": {key: item["status"] for key, item in evidence.items()},
        "platform_roas": {key: evidence[key]["value"] for key in PLATFORM_RATIOS},
        "source_kind": "PLATFORM_DIRECT",
        "request": {
            "report_type": "BASIC",
            "service_type": "AUCTION",
            "data_level": "AUCTION_CAMPAIGN" if level == "campaign" else "AUCTION_AD",
            "dimensions": [
                "campaign_id" if level == "campaign" else "ad_id_v2",
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
