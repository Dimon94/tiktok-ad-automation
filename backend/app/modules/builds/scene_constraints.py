"""Versioned platform facts and separately identified application copy policy.

An undocumented platform maximum stays None. This application independently
limits its English copy pool to 1..100 characters; it is not a TikTok quota.
"""

from typing import Any

REVISION = "minis-unit-constraints-2026-11-04-v6"
PLATFORM_COPY_LENGTH_LIMIT: int | None = None
COPY_LENGTH_LIMIT = 100
COPY_LENGTH_MEASUREMENT = "characters"


def constraints_for(
    currency: str, *, media_kind: str = "VIDEO"
) -> tuple[dict[str, Any], tuple[str, ...]]:
    # 预算是本地参数规则；历史广告组不是账户资格，新账户也能直接配置预算。
    reasons: list[str] = []
    if media_kind == "CAROUSEL":
        # 单账户历史创建不能证明本账户/本合同的轮播资格，待独立格式证据后开放。
        reasons.append("carousel_format_unverified")
    minimum, maximum, precision = "50", "10000000", "0.01"
    constraints: dict[str, Any] = {
        "revision": REVISION,
        "name_limits": {"campaign": 512, "adgroup": 512, "ad": 512},
        "name_measurement": {
            "campaign": "cjk_weighted",
            "adgroup": "characters",
            "ad": "cjk_weighted",
        },
        "emoji_allowed": False,
        "media_kind": media_kind,
        "max_creatives_per_ad": 50,
        "max_images_per_unit": 35 if media_kind == "CAROUSEL" else None,
        "max_ads_per_adgroup": 30,
        "roas_bid": {"minimum": "0.01", "maximum": "1000"},
        # 金额范围沿用现有 USD 规则，不再额外读取广告组目录。
        "adgroup_daily_budget": (
            {
                "currency": "USD",
                "minimum_inclusive": minimum,
                "maximum_exclusive": maximum,
                "precision": precision,
            }
            if currency == "USD"
            else None
        ),
        "budget_capabilities": {
            "campaign_daily_budget": "verified",
            "adgroup_daily_budget": "verified" if currency == "USD" else "unverified",
        },
        "bid_capabilities": {
            "HIGHEST_VALUE": {
                "optimization_goal": "VALUE",
                "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
                "deep_bid_type": "VO_HIGHEST_VALUE",
            },
            "TARGET_ROAS": {"deep_bid_type": "VO_MIN_ROAS"},
        },
        "platform_copy_length": PLATFORM_COPY_LENGTH_LIMIT,
        "copy_policy": {
            "source": "APPLICATION",
            "minimum": 1,
            "maximum": COPY_LENGTH_LIMIT,
            "measurement": COPY_LENGTH_MEASUREMENT,
        },
        "copy_length": COPY_LENGTH_LIMIT,
        "copy_measurement": COPY_LENGTH_MEASUREMENT,
    }
    # This narrowly verified currency is explicit, not inherited from account defaults.
    if currency == "USD":
        constraints["campaign_daily_budget"] = {
            "currency": "USD",
            "minimum_inclusive": "50",
            "maximum_exclusive": "10000000",
            "precision": "0.01",
        }
    else:
        reasons.append("budget_limits_unverified")
    return constraints, tuple(reasons)
