"""报表指标字典。

名称与平台字段一律取自 docs/contracts/tiktok-basic-report-metrics/v1.3.zh.md
（官方 v1.3 基础报表支持指标）。本地存储列 revenue_d3/d7/d14/d30 对应官方
第 2/6/13/29 天（dN = 点击后 (N+1)×24 小时），展示必须用官方天数。
素材报表是另一套字段和名称，不能套用基础报表的叫法。
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.errors import DomainError

Unit = Literal["money", "count", "ratio", "multiple"]
_PREFIX = "Native Growth "
NATIVE_CONTRACT = "native-growth-day-v1"
# 本地 creative 直接观测合同；不代表平台归因窗口或流量口径已确认。
CREATIVE_OBSERVED_CONTRACT = "creative-observed-v1"


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    unit: Unit
    additive: bool
    basic_field: str | None
    material_field: str | None
    basic_name_zh: str
    material_name_zh: str | None
    basic_name_en: str
    material_name_en: str | None


def _m(
    key: str,
    unit: Unit,
    additive: bool,
    basic: str | None,
    material: str | None,
    zh: str,
    mzh: str | None,
    en: str,
    men: str | None,
) -> MetricDefinition:
    return MetricDefinition(key, unit, additive, basic, material, zh, mzh, en, men)


METRIC_CATALOG: tuple[MetricDefinition, ...] = (
    _m("spend", "money", True, "spend", "spend", "消耗", "消耗", "Cost", "Cost"),
    _m(
        "revenue_total",
        "money",
        True,
        "native_growth_total_ad_impression_value",
        "onsite_total_ad_impression_value",
        "Native Growth 广告展示总价值",
        "广告曝光总价值",
        "Native Growth total ad impression value",
        "Total ad impression value",
    ),
    _m(
        "roas_total",
        "multiple",
        False,
        "native_growth_ad_revenue_roas",
        "onsite_ad_impression_ad_revenue_roas",
        "Native Growth 广告收益 ROAS",
        "广告收入 ROAS (TikTok)",
        "Native Growth ad revenue ROAS",
        "Ad revenue ROAS (TikTok)",
    ),
    _m(
        "revenue_d0",
        "money",
        True,
        "native_growth_ad_revenue_value_d0",
        "onsite_ad_impression_ad_revenue_day0",
        "Native Growth 第 0 天 广告收益金额",
        "第 0 天广告收入",
        "Native Growth day 0 ad revenue value",
        "Day 0 ad revenue",
    ),
    _m(
        "roas_d0",
        "multiple",
        False,
        "native_growth_ad_revenue_roas_d0",
        "onsite_ad_impression_ad_revenue_roas_day0",
        "Native Growth 第 0 天广告收益 ROAS",
        "第 0 天广告收入 ROAS",
        "Native Growth day 0 ad revenue ROAS",
        "Day 0 ad revenue ROAS",
    ),
    _m(
        "revenue_d3",
        "money",
        True,
        "native_growth_ad_revenue_value_d2",
        None,
        "Native Growth 第 2 天广告收益金额",
        None,
        "Native Growth day 2 ad revenue value",
        None,
    ),
    _m(
        "roas_d3",
        "multiple",
        False,
        "native_growth_ad_revenue_roas_d2",
        None,
        "Native Growth 第 2 天广告收益 ROAS",
        None,
        "Native Growth day 2 ad revenue ROAS",
        None,
    ),
    _m(
        "revenue_d7",
        "money",
        True,
        "native_growth_ad_revenue_value_d6",
        "onsite_ad_impression_ad_revenue_day6",
        "Native Growth 第 6 天广告收益金额",
        "第 6 天广告收入",
        "Native Growth day 6 ad revenue value",
        "Day 6 ad revenue",
    ),
    _m(
        "roas_d7",
        "multiple",
        False,
        "native_growth_ad_revenue_roas_d6",
        "onsite_ad_impression_ad_revenue_roas_day6",
        "Native Growth 第 6 天广告收益 ROAS",
        "第 6 天广告收入 ROAS",
        "Native Growth day 6 ad revenue ROAS",
        "Day 6 ad revenue ROAS",
    ),
    _m(
        "revenue_d14",
        "money",
        True,
        "native_growth_ad_revenue_value_d13",
        "onsite_ad_impression_ad_revenue_day13",
        "Native Growth 第 13 天广告收益金额",
        "第 13 天广告收入",
        "Native Growth day 13 ad revenue value",
        "Day 13 ad revenue",
    ),
    _m(
        "roas_d14",
        "multiple",
        False,
        "native_growth_ad_revenue_roas_d13",
        "onsite_ad_impression_ad_revenue_roas_day13",
        "Native Growth 第 13 天广告收益 ROAS",
        "第 13 天广告收入 ROAS",
        "Native Growth day 13 ad revenue ROAS",
        "Day 13 ad revenue ROAS",
    ),
    _m(
        "revenue_d30",
        "money",
        True,
        "native_growth_ad_revenue_value_d29",
        None,
        "Native Growth 第 29 天广告收益金额",
        None,
        "Native Growth day 29 ad revenue value",
        None,
    ),
    _m(
        "roas_d30",
        "multiple",
        False,
        "native_growth_ad_revenue_roas_d29",
        None,
        "Native Growth 第 29 天广告收入 ROAS",
        None,
        "Native Growth day 29 ad revenue ROAS",
        None,
    ),
    _m(
        "impressions",
        "count",
        True,
        "impressions",
        "impressions",
        "展示量",
        "展示量",
        "Impressions",
        "Impressions",
    ),
    _m(
        "clicks",
        "count",
        True,
        "clicks",
        "clicks",
        "点击量（目标页面）",
        "点击量（目标页面）",
        "Clicks (destination)",
        "Clicks (destination)",
    ),
    _m(
        "ctr",
        "ratio",
        False,
        "ctr",
        "ctr",
        "点击率（目标页面）",
        "点击率（目标页面）",
        "CTR (destination)",
        "CTR (destination)",
    ),
    _m(
        "cpm",
        "money",
        False,
        "cpm",
        "cpm",
        "千次展示成本 (CPM)",
        "千次展示成本 (CPM)",
        "CPM",
        "CPM",
    ),
    _m(
        "cpc",
        "money",
        False,
        "cpc",
        "cpc",
        "平均点击成本（目标页面）",
        "平均点击成本（目标页面）",
        "CPC (destination)",
        "CPC (destination)",
    ),
    _m(
        "ad_impression_events",
        "count",
        True,
        "native_growth_total_ad_impression_event_count",
        "onsite_total_ad_impression",
        "Native Growth 广告展示事件总数",
        "广告曝光事件总数",
        "Native Growth total ad impression events",
        "Total ad impression events",
    ),
    _m(
        "cost_per_ad_impression_event",
        "money",
        False,
        "native_growth_cost_per_ad_impression_event",
        "onsite_cost_per_ad_impression",
        "Native Growth 单次广告展示事件成本",
        "广告曝光事件平均成本",
        "Native Growth cost per ad impression event",
        "Cost per ad impression event",
    ),
)
_BY_KEY = {item.key: item for item in METRIC_CATALOG}
# 存储列：直接写入事实表的可加原始量；比率类在查询时重算，不落库。
STORED_KEYS = (
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
)


def metric_name_zh(key: str, dimension: str) -> str:
    item = _BY_KEY[key]
    if dimension == "material":
        return item.material_name_zh or item.basic_name_zh
    return item.basic_name_zh


def table_label(name: str) -> str:
    """表头省略 Native Growth 前缀；完整名放在悬停说明和导出。"""
    return name.removeprefix(_PREFIX)


def basic_fields() -> dict[str, str]:
    fields: dict[str, str] = {}
    for key in STORED_KEYS:
        field = _BY_KEY[key].basic_field
        # 入库指标都来自基础报表，缺字段是指标字典配置错误，必须立即暴露。
        if field is None:
            raise ValueError(f"stored metric {key} has no basic report field")
        fields[key] = field
    return fields


def material_fields() -> dict[str, str]:
    return {
        key: field
        for key in STORED_KEYS
        if (field := _BY_KEY[key].material_field) is not None
    }


# 派生比率：由同口径合计的分子、分母重算，报表与自动规则共用，不平均各行比率。
DERIVED_RATIOS: tuple[tuple[str, str, str, int], ...] = (
    *(
        (f"roas_{d}", f"revenue_{d}", "spend", 1)
        for d in ("d0", "d3", "d7", "d14", "d30", "total")
    ),
    ("ctr", "clicks", "impressions", 1),
    ("cpm", "spend", "impressions", 1000),
    ("cpc", "spend", "clicks", 1),
    ("cost_per_ad_impression_event", "spend", "ad_impression_events", 1),
)


class MetricContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    revision: str
    field: str | None
    unit: Literal["MONEY", "RATIO", "COUNT"]
    traffic_scope: Literal["ALL_TRAFFIC", "PAID", "UNCONFIRMED"]
    window: Literal[
        "NOT_APPLICABLE",
        "TOTAL_CUTOFF_UNKNOWN",
        "CLICK_HOURS",
        "CALENDAR_DAY",
        "UNCONFIRMED",
    ]
    click_hours: int | None
    aggregation: Literal["SUM_SAME_LEVEL", "RATIO_OF_SUMS", "SINGLE_OBSERVATION_ONLY"]
    definition_ref: str
    numerator: str | None = None
    denominator: str | None = None


def semantics_for(
    key: str,
    revision: str,
    *,
    grain: Literal["campaign", "adgroup", "ad", "creative", "material_instance"]
    | None = None,
) -> MetricContract:
    """未知版本拒绝解释；不按当前命名追认历史金额。"""
    creative = (
        grain in {"creative", "material_instance"}
        and revision == CREATIVE_OBSERVED_CONTRACT
    )
    if revision != NATIVE_CONTRACT and not creative:
        raise DomainError("report_query_invalid", "未知指标合同修订")
    item = next(item for item in METRIC_CATALOG if item.key == key)
    ratio = next((r for r in DERIVED_RATIOS if r[0] == key), None)
    native = key.startswith(("revenue_", "roas_")) or (
        creative and key in {"ad_impression_events", "cost_per_ad_impression_event"}
    )
    window_days = next((n for n in (0, 3, 7, 14, 30) if key.endswith(f"_d{n}")), None)
    return MetricContract(
        revision=revision,
        field=None if ratio else item.basic_field,
        unit="MONEY"
        if item.unit == "money"
        else "COUNT"
        if item.unit == "count"
        else "RATIO",
        traffic_scope="UNCONFIRMED" if native else "PAID",
        window="UNCONFIRMED"
        if creative and native
        else "CLICK_HOURS"
        if native and window_days is not None
        else "TOTAL_CUTOFF_UNKNOWN"
        if native
        else "NOT_APPLICABLE",
        click_hours=None
        if creative
        else 24
        if window_days == 0
        else window_days * 24
        if window_days is not None
        else None,
        aggregation="RATIO_OF_SUMS" if ratio else "SUM_SAME_LEVEL",
        definition_ref="local-observed-fields; platform creative definition UNCONFIRMED"
        if creative and native
        else "tiktok-basic-report-metrics/v1.3.zh.md",
        numerator=ratio[1] if ratio else None,
        denominator=ratio[2] if ratio else None,
    )


def platform_ratio_contract(key: str) -> MetricContract | None:
    if key not in {"roas_total", "roas_d0"}:
        return None
    return semantics_for(key, NATIVE_CONTRACT).model_copy(
        update={
            "field": "native_growth_ad_revenue_roas_d0"
            if key == "roas_d0"
            else "native_growth_ad_revenue_roas",
            "aggregation": "SINGLE_OBSERVATION_ONLY",
            "numerator": None,
            "denominator": None,
        }
    )
