"""只对已发布系列日经营投影做SQL汇总，分页与卡片共用同一版本。"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.accounts.models import AdvertiserAccount
from app.modules.business.calculation import (
    AMOUNT_KEYS,
    SUBSIDY_KEYS,
    ZERO,
    derive_metrics,
    quarter_start,
)
from app.modules.business.configuration import scope_accounts
from app.modules.business.models import (
    BusinessRun,
    BusinessScope,
)
from app.modules.business.projection import _days, _source_complete, next_quarter
from app.modules.business.publication import cohort_signature
from app.modules.business.query_plan import (
    build_query_plan,
    nullable_sum,
    sort_expression,
)
from app.modules.business.schemas import BusinessRow, DashboardFilter, DashboardPublic


def resolve_runs(
    session: Session, context: TenantContext, filters: DashboardFilter
) -> tuple[list[str], list[str], date, Sequence[BusinessRun]]:
    allowed = scope_accounts(session, context, filters.bc_id)
    selected = filters.advertiser_ids or allowed
    if not set(selected) <= set(allowed):
        raise HTTPException(403, "筛选包含未授权账户")
    accounts = (
        session.exec(
            select(AdvertiserAccount).where(
                col(AdvertiserAccount.tenant_id) == context.tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(selected),
            )
        ).all()
        if selected
        else []
    )
    now = datetime.now(UTC)
    cutoff = min(
        (
            now.astimezone(ZoneInfo(a.timezone)).date() - timedelta(days=1)
            for a in accounts
        ),
        default=now.date() - timedelta(days=1),
    )
    end = min(filters.end_date, cutoff)
    statement = (
        select(BusinessRun)
        .where(
            col(BusinessRun.tenant_id) == context.tenant_id,
            col(BusinessRun.bc_id) == filters.bc_id,
            col(BusinessRun.quarter) <= end,
            col(BusinessRun.end_date) >= filters.start_date,
            col(BusinessRun.status) == "COMPLETE",
        )
        .with_for_update(read=True, of=BusinessRun)
    )
    if filters.run_ids is not None:
        if len(set(filters.run_ids)) != len(filters.run_ids):
            raise HTTPException(422, "重复计算版本")
        runs = session.exec(
            statement.where(col(BusinessRun.id).in_(filters.run_ids))
        ).all()
        if len(runs) != len(filters.run_ids) or len({r.quarter for r in runs}) != len(
            runs
        ):
            raise HTTPException(409, "计算版本不存在或范围不一致，请刷新")
    else:
        scope = session.get(
            BusinessScope, (context.tenant_id, filters.bc_id), populate_existing=True
        )
        ids = [UUID(value) for value in scope.published_runs.values()] if scope else []
        runs = session.exec(statement.where(col(BusinessRun.id).in_(ids))).all()
    if (
        len({r.scope_revision for r in runs}) > 1
        or len({cohort_signature(r) for r in runs}) > 1
    ):
        raise HTTPException(409, "计算版本混用商业配置修订，请刷新")
    return allowed, selected, end, runs


def query_dashboard(
    session: Session, *, context: TenantContext, filters: DashboardFilter
) -> DashboardPublic:
    allowed, selected, end, runs = resolve_runs(session, context, filters)
    plan = build_query_plan(
        session,
        context=context,
        filters=filters,
        selected=selected,
        allowed=allowed,
        end=end,
        runs=list(runs),
    )
    B, predicates, grouped = plan.model, plan.predicates, plan.grouped
    run_ids = [r.id for r in runs]
    total = session.exec(select(func.count()).select_from(grouped)).one()
    order = sort_expression(grouped, filters)
    rows = (
        session.execute(
            sa_select(grouped)
            .order_by(
                order.desc().nulls_last()
                if filters.descending
                else order.asc().nulls_last(),
                grouped.c.key,
            )
            .offset(filters.offset)
            .limit(filters.limit)
        )
        .mappings()
        .all()
    )
    raw_totals = (
        session.execute(
            sa_select(
                *[nullable_sum(getattr(grouped.c, k)).label(k) for k in AMOUNT_KEYS]
            ).select_from(grouped)
        )
        .mappings()
        .one()
    )
    missing: set[date] = set()
    expected = quarter_start(filters.start_date)
    by_quarter = {r.quarter: r for r in runs}
    while expected <= end:
        r = by_quarter.get(expected)
        if r is None:
            missing.update(
                _days(
                    max(expected, filters.start_date),
                    min(next_quarter(expected) - timedelta(days=1), end),
                )
            )
        else:
            missing.update(
                d
                for d in _days(max(expected, filters.start_date), min(r.end_date, end))
                if any(
                    a not in r.inputs["accounts"] or not _source_complete(r, a, d)
                    for a in selected
                )
            )
            if r.end_date < min(next_quarter(expected) - timedelta(days=1), end):
                missing.update(
                    _days(
                        r.end_date + timedelta(days=1),
                        min(next_quarter(expected) - timedelta(days=1), end),
                    )
                )
        expected = next_quarter(expected)
    issues = set(plan.policy_issues)
    if missing:
        issues.add("SOURCE_INCOMPLETE")
    if runs and any(
        not set(r.inputs["accounts"]) <= set(allowed)
        or r.inputs.get("policy_restricted")
        for r in runs
    ):
        issues.add("POLICY_SCOPE_RESTRICTED")
    restricted = "POLICY_SCOPE_RESTRICTED" in issues
    all_issues = (
        session.execute(
            select(func.jsonb_array_elements_text(col(B.issues)))
            .where(*predicates)
            .distinct()
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    issues.update(all_issues)
    if "COMPENSATION_EVIDENCE_MISSING" in issues:
        issues.add("COMPENSATION_PARTIAL")

    def values(raw: Any) -> dict[str, Decimal | None]:
        result = {k: raw[k] for k in AMOUNT_KEYS}
        if restricted:
            result.update(dict.fromkeys(SUBSIDY_KEYS))
        return derive_metrics(result, scenario=filters.scenario, issues=issues)

    totals = dict(raw_totals)
    if not total and not missing:
        totals = dict.fromkeys(AMOUNT_KEYS, ZERO)
    if missing or plan.policy_unknown:
        # 缺日报会漏掉未知赔付对象/跨档，不能把已知部分当作完整预估。
        totals.update(dict.fromkeys(SUBSIDY_KEYS))
        issues.add("COMPENSATION_PARTIAL")
    currencies = (
        session.exec(
            select(col(B.currency))
            .where(
                col(B.tenant_id) == context.tenant_id,
                col(B.run_id).in_(run_ids),
                col(B.advertiser_id).in_(selected),
                col(B.stat_date) >= filters.start_date,
                col(B.stat_date) <= end,
            )
            .distinct()
        ).all()
        if runs
        else []
    )
    scope = session.get(BusinessScope, (context.tenant_id, filters.bc_id))
    pending = bool(scope and scope.published_revision != scope.revision) or bool(
        session.exec(
            select(col(BusinessRun.id))
            .where(
                col(BusinessRun.tenant_id) == context.tenant_id,
                col(BusinessRun.bc_id) == filters.bc_id,
                col(BusinessRun.quarter) <= end,
                col(BusinessRun.end_date) >= filters.start_date,
                col(BusinessRun.status).in_(["QUEUED", "RUNNING"]),
            )
            .limit(1)
        ).first()
    )
    return DashboardPublic(
        rows=[
            BusinessRow(
                key=r["key"],
                name=r["name"],
                provider_name=r["provider_name"],
                provider_id=UUID(r["provider_id"]) if r["provider_id"] else None,
                drama_id=UUID(r["drama_id"])
                if r["drama_id"] and filters.dimension in {"campaign", "drama"}
                else None,
                advertiser_id=r["advertiser_id"]
                if filters.dimension == "campaign" or r["key"].startswith("unmapped:")
                else None,
                campaign_id=r["campaign_id"]
                if filters.dimension == "campaign" or r["key"].startswith("unmapped:")
                else None,
                values=values(r),
            )
            for r in rows
        ],
        totals=values(totals),
        total=total,
        run_ids=run_ids,
        currency=filters.currency,
        currencies=sorted(currencies),
        end_date=end,
        updated_at=min((r.finished_at for r in runs if r.finished_at), default=None),
        issues=sorted(issues),
        missing_dates=sorted(missing),
        pending=pending,
    )
