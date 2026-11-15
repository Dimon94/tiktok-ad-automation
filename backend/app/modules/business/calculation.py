"""纯Decimal核算；未知值保留，三个补贴情景从不相加。"""

from collections.abc import Collection, Mapping
from datetime import date
from decimal import Decimal
from typing import Literal

ZERO = Decimal(0)
SUBSIDY_KEYS = ("subsidy_comic", "subsidy_other", "subsidy_existing")
AMOUNT_KEYS = (
    "spend",
    "revenue",
    "provider_share",
    "net_revenue",
    "rebate",
    "business_revenue",
    "compensation",
    *SUBSIDY_KEYS,
)


def business_amounts(
    spend: Decimal | None,
    revenue: Decimal | None,
    share: Decimal | None,
    rebate: Decimal | None,
) -> dict[str, Decimal | None]:
    if any(
        r is not None and (not r.is_finite() or not ZERO <= r <= 1)
        for r in (share, rebate)
    ):
        raise ValueError("比例必须在0到1之间")
    deduction = None if revenue is None or share is None else revenue * share
    net = None if revenue is None or share is None else revenue * (1 - share)
    returned = None if spend is None or rebate is None else spend * rebate
    return {
        "spend": spend,
        "revenue": revenue,
        "provider_share": deduction,
        "net_revenue": net,
        "rebate": returned,
        "business_revenue": None if net is None or returned is None else net + returned,
    }


def tier_amounts(
    spend: Decimal,
    day: date,
    currency: str,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    # Q4沿用Q3来自本轮用户确认，不能向其它季度自动续用。
    if currency != "USD" or not date(2026, 7, 1) <= day <= date(2026, 12, 31):
        return None, None, None
    for threshold, values in (
        (30000, (9000, 7500, 2000)),
        (10000, (4000, 2500, 2000)),
        (5000, (2000, 1250, 1000)),
        (2000, (800, 500, 400)),
        (1000, (0, 0, 200)),
        (0, (0, 0, 0)),
    ):
        if spend >= threshold:
            first, second, third = values
            return Decimal(first), Decimal(second), Decimal(third)
    raise ValueError("累计消耗不能为负")


def compensation_amount(
    spend: Decimal | None,
    revenue_d0: Decimal | None,
    bid: Decimal | None,
    events: Decimal | None,
    eligible: bool | None,
) -> Decimal | None:
    if eligible is False or spend == 0 or events is not None and events < 300:
        return ZERO
    if eligible is None or any(v is None for v in (spend, revenue_d0, bid, events)):
        return None
    assert spend is not None and revenue_d0 is not None and events is not None
    if bid is None or bid <= 0:
        return None
    if events < 300 or bid * spend <= Decimal("1.2") * revenue_d0:
        return ZERO
    return max(ZERO, spend - revenue_d0 / bid)


def allocate_amount(amount: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """稳定末项承接Decimal除法尾差，支持历史修正的负增量。"""
    if any(v < 0 for v in weights.values()):
        raise ValueError("消耗权重不能为负")
    total = sum(weights.values(), ZERO)
    if total == 0:
        if amount != 0:
            raise ValueError("非零补贴缺少分摊消耗")
        return dict.fromkeys(weights, ZERO)
    result = {key: amount * weights[key] / total for key in sorted(weights)}
    last = next(reversed(result))
    result[last] += amount - sum(result.values(), ZERO)
    return result


def quarter_start(day: date) -> date:
    return date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)


def derive_metrics(
    values: Mapping[str, Decimal | None],
    *,
    scenario: Literal["base", "comic", "other", "existing"],
    issues: Collection[str],
) -> dict[str, Decimal | None]:
    values = dict(values)
    spend = values["spend"]
    values["platform_roas"] = (
        values["revenue"] / spend if spend and values["revenue"] is not None else None
    )
    business = values["business_revenue"]
    values["business_roas"] = (
        business / spend if spend and business is not None else None
    )
    if scenario != "base":
        subsidy = values[f"subsidy_{scenario}"]
        comp = values["compensation"]
        business = (
            business + subsidy + comp
            if business is not None and subsidy is not None and comp is not None
            else None
        )
    values["scenario_revenue"] = business
    values["scenario_roas"] = (
        business / spend if spend and business is not None else None
    )
    # 赔付小计保留，缺证仅屏蔽含预计情景；issues 是唯一完整性事实。
    if scenario != "base" and "COMPENSATION_PARTIAL" in issues:
        values.update(scenario_revenue=None, scenario_roas=None)
    return values
