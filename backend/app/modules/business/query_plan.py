"""列表、卡片和导出共用的聚合SQL；政策缺口独立于当天是否有事实行。"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import (
    Text,
    and_,
    any_,
    bindparam,
    case,
    cast,
    false,
    func,
    literal,
    or_,
)
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.sql.selectable import Subquery
from sqlmodel import Session, col

from app.core.context import TenantContext
from app.modules.business.calculation import AMOUNT_KEYS, SUBSIDY_KEYS
from app.modules.business.models import BusinessDay, BusinessRun, BusinessSummaryDay
from app.modules.business.schemas import DashboardFilter
from app.modules.reporting.filters import _like_literal


def nullable_sum(column: Any) -> Any:
    if getattr(column, "key", None) == "compensation":
        return func.sum(column)
    return case((func.count(column) < func.count(), None), else_=func.sum(column))


@dataclass
class QueryPlan:
    model: Any
    predicates: list[Any]
    grouped: Subquery
    policy_unknown: bool
    policy_issues: set[str]


def build_query_plan(
    session: Session,
    *,
    context: TenantContext,
    filters: DashboardFilter,
    selected: list[str],
    allowed: list[str],
    end: date,
    runs: list[BusinessRun],
) -> QueryPlan:
    B: Any = BusinessDay
    if (
        filters.dimension in {"drama", "provider"}
        and not filters.campaign_id
        and not filters.drama_id
        and not filters.drama_key
        and not filters.query.strip()
    ):
        B = BusinessSummaryDay
    run_ids = [r.id for r in runs]
    predicates = [
        col(B.tenant_id) == context.tenant_id,
        col(B.bc_id) == filters.bc_id,
        col(B.run_id).in_(run_ids),
        col(B.advertiser_id).in_(selected),
        col(B.stat_date) >= filters.start_date,
        col(B.stat_date) <= end,
        col(B.currency) == filters.currency,
    ]
    if B is BusinessSummaryDay:
        predicates.append(col(B.kind) == filters.dimension)
    if filters.provider_id:
        predicates.append(col(B.provider_id) == filters.provider_id)
    if filters.unmapped_only:
        predicates.append(col(B.provider_id).is_(None))
    if filters.drama_key:
        predicates.append(col(B.drama_key) == filters.drama_key)
    if filters.drama_id:
        predicates.append(col(B.drama_id) == filters.drama_id)
    if filters.campaign_id:
        predicates.append(col(B.campaign_id) == filters.campaign_id)
    if filters.query.strip():
        pattern = f"%{_like_literal(filters.query.strip())}%"
        predicates.append(
            or_(
                col(B.campaign_name).ilike(pattern, escape="\\"),
                col(B.drama_name).ilike(pattern, escape="\\"),
                col(B.provider_name).ilike(pattern, escape="\\"),
            )
        )
    campaign_drama_keys = None
    if filters.campaign_id:
        campaign_drama_keys = set(
            typing_cast(SQLAlchemySession, session)
            .execute(
                sa_select(col(BusinessDay.drama_key))
                .where(
                    col(BusinessDay.tenant_id) == context.tenant_id,
                    col(BusinessDay.bc_id) == filters.bc_id,
                    col(BusinessDay.run_id).in_(run_ids),
                    col(BusinessDay.advertiser_id).in_(selected),
                    col(BusinessDay.campaign_id) == filters.campaign_id,
                )
                .distinct()
            )
            .scalars()
            .all()
        )
    masks = []
    uncertain_selection = False
    issues = set()
    for run in runs:
        high = min(end, run.end_date)
        if high < max(run.quarter, filters.start_date):
            continue
        prefix_gaps = sorted(
            d
            for d in run.coverage.get("missing_dates", [])
            if date.fromisoformat(d) <= high
        )
        restricted = run.inputs.get("policy_restricted") or not set(
            run.inputs["accounts"]
        ) <= set(allowed)
        unsupported = not date(2026, 7, 1) <= run.quarter <= date(2026, 10, 1)
        if prefix_gaps or restricted or unsupported:
            condition: Any = col(B.run_id) == run.id
            if (
                filters.dimension == "day"
                and prefix_gaps
                and not restricted
                and not unsupported
            ):
                condition = and_(
                    condition, col(B.stat_date) >= date.fromisoformat(prefix_gaps[0])
                )
            masks.append(condition)
            uncertain_selection = True
            issues.add(
                "SUBSIDY_POLICY_UNCONFIGURED"
                if unsupported
                else "SUBSIDY_SCOPE_INCOMPLETE"
            )
            continue
        by_date: dict[str, set[str]] = defaultdict(set)
        for drama_key, info in run.coverage.get("subsidy_unknown", {}).items():
            if date.fromisoformat(info["from"]) > high:
                continue
            relevant = (
                not filters.drama_id or str(filters.drama_id) == drama_key
            ) and (not filters.drama_key or filters.drama_key == drama_key)
            relevant &= (
                not filters.provider_id or str(filters.provider_id) in info["providers"]
            )
            relevant &= campaign_drama_keys is None or drama_key in campaign_drama_keys
            if relevant:
                uncertain_selection = True
                issues.add(info["reason"])
            slot = info["from"] if filters.dimension == "day" else "all"
            by_date[slot].update(
                info["providers"]
                if B is BusinessSummaryDay and filters.dimension == "provider"
                else [drama_key]
            )
        for cutoff, identities in by_date.items():
            column = (
                func.coalesce(cast(col(B.provider_id), Text), literal("unmapped"))
                if B is BusinessSummaryDay and filters.dimension == "provider"
                else col(B.drama_key)
            )
            identity = column == any_(
                bindparam(None, sorted(identities), type_=ARRAY(Text))
            )
            condition = and_(col(B.run_id) == run.id, identity)
            if filters.dimension == "day":
                condition = and_(
                    condition, col(B.stat_date) >= date.fromisoformat(cutoff)
                )
            masks.append(condition)
    masked = or_(*masks) if masks else false()
    keys: dict[str, tuple[Any, Any]] = {
        "campaign": (
            func.concat(col(B.advertiser_id), "/", col(B.campaign_id)),
            col(B.campaign_name),
        ),
        "drama": (col(B.drama_key), col(B.drama_name)),
        "provider": (
            func.coalesce(cast(col(B.provider_id), Text), literal("unmapped")),
            col(B.provider_name),
        ),
        "day": (cast(col(B.stat_date), Text), cast(col(B.stat_date), Text)),
    }
    key, name = keys[filters.dimension]
    grouped = (
        sa_select(
            key.label("key"),
            func.max(name).label("name"),
            func.min(col(B.drama_key)).label("rounding_drama_key"),
            func.max(col(B.provider_name)).label("provider_name"),
            func.min(cast(col(B.provider_id), Text)).label("provider_id"),
            func.min(cast(col(B.drama_id), Text)).label("drama_id"),
            func.min(col(B.advertiser_id)).label("advertiser_id"),
            func.min(col(B.campaign_id)).label("campaign_id"),
            *[
                nullable_sum(
                    case((masked, None), else_=getattr(B, k))
                    if k in SUBSIDY_KEYS
                    else getattr(B, k)
                ).label(k)
                for k in AMOUNT_KEYS
            ],
        )
        .where(*predicates)
        .group_by(key)
        .subquery()
    )
    return QueryPlan(B, predicates, grouped, uncertain_selection, issues)


def sort_expression(grouped: Subquery, filters: DashboardFilter) -> Any:
    """排序与导出都在SQL聚合后执行；ROAS按合计比值排序，空值置后。"""
    if filters.sort_by in {"platform_roas", "business_roas"}:
        numerator = (
            grouped.c.revenue
            if filters.sort_by == "platform_roas"
            else grouped.c.business_revenue
        )
        return numerator / func.nullif(grouped.c.spend, 0)
    return getattr(grouped.c, filters.sort_by)
