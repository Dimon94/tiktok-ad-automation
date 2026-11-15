"""有界T+1核算：每次一页系列日或一天补贴，版本完成后原子切换。"""

from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from typing import cast as typing_cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Text,
    Uuid,
    and_,
    case,
    cast,
    func,
    insert,
    literal,
    or_,
    text,
    tuple_,
)
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.accounts.models import AdvertiserAccount, TenantBC
from app.modules.business.calculation import (
    AMOUNT_KEYS,
    SUBSIDY_KEYS,
    ZERO,
    allocate_amount,
    business_amounts,
    compensation_amount,
    quarter_start,
    tier_amounts,
)
from app.modules.business.configuration import scope_accounts
from app.modules.business.identity import (
    catalog_snapshot,
    identify_campaign,
    unresolved_name,
)
from app.modules.business.models import (
    BusinessDay,
    BusinessMember,
    BusinessPublication,
    BusinessRun,
    BusinessScope,
    BusinessSummaryDay,
)
from app.modules.business.prefix import (
    KNOWN_PHASES,
    PREPARATION_PHASES,
    SNAPSHOT_LOCK,
    advance_prefix,
    advance_rebase,
    begin_prefix,
    begin_rebase,
    frozen_accounts,
    frozen_metadata,
    narrow_runs,
    rebase_dirty_start,
)
from app.modules.business.rate_periods import RatePeriod, period_rate
from app.modules.business.source import (
    discover_exact_mappings,
    mapping_catalog,
    source_snapshot,
)
from app.modules.reporting.store_models import CampaignReportDay

BASE_BATCH = 500


def next_quarter(day: date) -> date:
    return date(
        day.year + (day.month == 10), 1 if day.month == 10 else day.month + 3, 1
    )


def _scope_lock(session: Session, context: TenantContext, bc_id: str) -> None:
    # 只串行化经营写事务，不修改BC主键；NO KEY UPDATE保留互斥，
    # 同时允许目录/报表创建时对BC外键做KEY SHARE校验。
    session.exec(
        select(TenantBC)
        .where(
            col(TenantBC.tenant_id) == context.tenant_id, col(TenantBC.bc_id) == bc_id
        )
        .with_for_update(key_share=True)
    ).one()


def request_rebuild(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    start_date: date,
    end_date: date,
    now: datetime | None = None,
    defer_snapshot: bool = False,
    coalesce_pending: bool = False,
) -> list[BusinessRun]:
    now = now or datetime.now(UTC)
    allowed = scope_accounts(session, context, bc_id)
    if not allowed:
        return []
    _scope_lock(session, context, bc_id)
    accounts = session.exec(
        select(AdvertiserAccount).where(
            col(AdvertiserAccount.tenant_id) == context.tenant_id,
            col(AdvertiserAccount.advertiser_id).in_(allowed),
        )
    ).all()
    end_date = min(
        end_date,
        max(
            now.astimezone(ZoneInfo(a.timezone)).date() - timedelta(days=1)
            for a in accounts
        ),
    )
    if start_date > end_date:
        return []
    scope = session.get(BusinessScope, (context.tenant_id, bc_id))
    fresh = scope is None
    if scope is None:
        oldest = session.exec(
            select(func.min(col(CampaignReportDay.stat_date))).where(
                col(CampaignReportDay.tenant_id) == context.tenant_id,
                col(CampaignReportDay.advertiser_id).in_(allowed),
            )
        ).one()
        scope = BusinessScope(
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            actor_id=context.actor_id,
            start_date=quarter_start(min(start_date, oldest or start_date)),
        )
        session.add(scope)
        session.flush()
    scope.actor_id = context.actor_id
    members = session.exec(
        select(BusinessMember).where(
            col(BusinessMember.tenant_id) == context.tenant_id,
            col(BusinessMember.bc_id) == bc_id,
        )
    ).all()
    known = {m.advertiser_id for m in members}
    for account in accounts:
        if account.advertiser_id not in known:
            session.add(
                BusinessMember(
                    tenant_id=context.tenant_id,
                    bc_id=bc_id,
                    advertiser_id=account.advertiser_id,
                    start_date=scope.start_date
                    if fresh
                    else now.astimezone(ZoneInfo(account.timezone)).date(),
                )
            )
            if not fresh:
                scope.revision += 1
    session.add(scope)
    session.flush()
    discover_exact_mappings(session, scope, allowed)
    # 剧库与解析规则变更必须整段重算，并让已发布季度统一切换，不能复用旧待归属前缀。
    catalog = catalog_snapshot(session, scope.tenant_id)
    if scope.published_revision == scope.revision and any(
        old
        and (
            old.get("calculation_revision") != 4
            or old.get("identity_catalog") != catalog
        )
        for old in (
            frozen_metadata(session, UUID(value))
            for value in scope.published_runs.values()
        )
    ):
        scope.revision += 1
        session.add(scope)
    results = []
    snapshot: dict[str, Any]
    targets = {}
    quarter = quarter_start(start_date)
    while quarter <= end_date:
        targets[quarter] = min(next_quarter(quarter) - timedelta(days=1), end_date)
        quarter = next_quarter(quarter)
    if (
        scope.published_revision is not None
        and scope.published_revision != scope.revision
    ):
        # 同一配置修订的已发布季度一起准备，避免跨季度查询混用新旧费率。
        for key, run_id in scope.published_runs.items():
            old = session.exec(
                narrow_runs().where(col(BusinessRun.id) == UUID(run_id))
            ).first()
            if old is not None:
                day = date.fromisoformat(key)
                targets[day] = max(targets.get(day, old.end_date), old.end_date)
    for quarter, stop in sorted(targets.items()):
        publication = session.get(
            BusinessPublication, (scope.tenant_id, bc_id, quarter)
        )
        published = (
            session.exec(
                narrow_runs().where(col(BusinessRun.id) == publication.run_id)
            ).first()
            if publication
            else None
        )
        if published is not None:
            # 查询较短区间不得让同季度发布指针倒退，丢掉已生成的后续日期。
            stop = max(stop, published.end_date)
        pending_end = session.exec(
            select(func.max(col(BusinessRun.end_date))).where(
                col(BusinessRun.tenant_id) == scope.tenant_id,
                col(BusinessRun.bc_id) == bc_id,
                col(BusinessRun.quarter) == quarter,
                col(BusinessRun.status).in_(["QUEUED", "RUNNING"]),
            )
        ).one()
        if pending_end is not None:
            stop = max(stop, pending_end)
        if coalesce_pending:
            pending = session.exec(
                narrow_runs()
                .where(
                    col(BusinessRun.tenant_id) == scope.tenant_id,
                    col(BusinessRun.bc_id) == bc_id,
                    col(BusinessRun.quarter) == quarter,
                    col(BusinessRun.scope_revision) == scope.revision,
                    col(BusinessRun.actor_id) == context.actor_id,
                    col(BusinessRun.status).in_(["QUEUED", "RUNNING"]),
                )
                .order_by(col(BusinessRun.created_at).desc())
                .limit(1)
            ).first()
            if pending is not None:
                # 定时检查继续已有冻结工作；更长区间留待其结束后的下一次检查。
                results.append(pending)
                continue
        if defer_snapshot:
            snapshot, signature = {}, ""
        else:
            snapshot, signature = source_snapshot(session, scope, quarter, stop, now)
        # 候选匹配只查身份；不为签名/修订判断传输完整inputs。
        candidate_id = session.exec(
            select(col(BusinessRun.id))
            .where(
                col(BusinessRun.tenant_id) == scope.tenant_id,
                col(BusinessRun.bc_id) == bc_id,
                col(BusinessRun.quarter) == quarter,
                col(BusinessRun.end_date) == stop,
                col(BusinessRun.source_signature) == signature,
                or_(
                    and_(
                        col(BusinessRun.status).in_(["QUEUED", "RUNNING"]),
                        col(BusinessRun.actor_id) == context.actor_id,
                    ),
                    and_(
                        col(BusinessRun.status) == "COMPLETE",
                        col(BusinessRun.id)
                        == (publication.run_id if publication else None),
                    ),
                ),
            )
            .order_by(col(BusinessRun.created_at).desc())
            .limit(1)
        ).first()
        candidate = (
            session.exec(
                narrow_runs().where(col(BusinessRun.id) == candidate_id)
            ).first()
            if candidate_id and not defer_snapshot
            else None
        )
        if candidate is None:
            # 每个季度最多一份候选；失效候选保留证据但不能再发布。
            old_candidates = session.exec(
                narrow_runs().where(
                    col(BusinessRun.tenant_id) == scope.tenant_id,
                    col(BusinessRun.bc_id) == bc_id,
                    col(BusinessRun.quarter) == quarter,
                    col(BusinessRun.status).in_(["QUEUED", "RUNNING"]),
                )
            ).all()
            for old in old_candidates:
                old.status = "SUPERSEDED"
                session.add(old)
            candidate = BusinessRun(
                tenant_id=scope.tenant_id,
                bc_id=bc_id,
                actor_id=context.actor_id,
                quarter=quarter,
                end_date=stop,
                cursor=quarter,
                scope_revision=scope.revision,
                inputs={**snapshot, "dirty_start": quarter.isoformat(), "after": None}
                if not defer_snapshot
                else {},
                phase="SNAPSHOT" if defer_snapshot else "BASE",
                source_signature=signature,
            )
            session.add(candidate)
            session.flush()
            if not defer_snapshot:
                begin_prefix(session, candidate)
        results.append(candidate)
    return results


def _source_complete(run: BusinessRun, advertiser: str, day: date) -> bool:
    meta = run.inputs["accounts"][advertiser]
    if day < date.fromisoformat(meta["start"]) or (
        meta["end"] and day > date.fromisoformat(meta["end"])
    ):
        return True
    if day > date.fromisoformat(meta["complete_through"]):
        return False
    state = run.inputs["states"].get(f"{advertiser}/{day.isoformat()}")
    if not state or state["status"] != "COMPLETE":
        return False
    observed = datetime.fromisoformat(state["observed"])
    day_end = datetime.combine(
        day + timedelta(days=1), time.min, ZoneInfo(meta["timezone"])
    )
    return observed >= day_end


def _prefix_complete(run: BusinessRun, day: date) -> bool:
    if run.inputs.get("policy_restricted"):
        return False
    current = run.quarter
    while current <= day:
        if any(not _source_complete(run, a, current) for a in run.inputs["accounts"]):
            return False
        current += timedelta(days=1)
    return True


def _base_page(session: Session, run: BusinessRun) -> None:
    allowed = [
        a
        for a, meta in run.inputs["accounts"].items()
        if date.fromisoformat(meta["start"]) <= run.cursor
        and (not meta["end"] or date.fromisoformat(meta["end"]) >= run.cursor)
        and _source_complete(run, a, run.cursor)
    ]
    statement = select(CampaignReportDay).where(
        col(CampaignReportDay.tenant_id) == run.tenant_id,
        col(CampaignReportDay.advertiser_id).in_(allowed),
        col(CampaignReportDay.stat_date) == run.cursor,
    )
    after = run.base_after
    if after:
        statement = statement.where(
            tuple_(
                col(CampaignReportDay.advertiser_id), col(CampaignReportDay.campaign_id)
            )
            > tuple(after)
        )
    facts = session.exec(
        statement.order_by(
            col(CampaignReportDay.advertiser_id), col(CampaignReportDay.campaign_id)
        ).limit(BASE_BATCH)
    ).all()
    if not facts:
        run.base_after = None
        run.cursor += timedelta(days=1)
        return
    pairs = [(f.advertiser_id, f.campaign_id) for f in facts]
    mappings = mapping_catalog(session, run.tenant_id, run.bc_id, pairs)
    # 只读取本任务冻结的完整日程，不能在逐日处理中混入刚修改的配置。
    effective = {
        key: period_rate(
            [RatePeriod.model_validate(p) for p in schedule["periods"]], run.cursor
        )
        for key, schedule in run.inputs["rate_schedules"].items()
    }
    rows = []
    catalog = run.inputs.get("identity_catalog", {"providers": [], "dramas": []})
    evidence_cache: dict[Any, Any] = {}
    for f in facts:
        mapping = mappings.get((f.advertiser_id, f.campaign_id)) or identify_campaign(
            f.campaign_name or "", catalog
        )
        identity = mapping or {
            "drama_id": None,
            "drama_key": f"unmapped:{f.advertiser_id}:{f.campaign_id}",
            "drama_name": unresolved_name(f.campaign_name or f.campaign_id, catalog),
            "provider_id": None,
            "provider_name": "待归属",
        }
        # 同一份解析结果同时用于金额与冻结明细，避免证据费率与金额口径分离。
        provider_rate = (
            effective.get(f"provider:{identity['provider_id']}", ZERO)
            if identity["provider_id"] is not None
            else None
        )
        rebate_rate = effective.get(f"rebate:{run.bc_id}", ZERO)
        values = business_amounts(f.spend, f.revenue_total, provider_rate, rebate_rate)
        issues = []
        if identity["provider_id"] is not None and provider_rate is None:
            issues.append("PROVIDER_RATE_MISSING")
        if rebate_rate is None:
            issues.append("REBATE_RATE_MISSING")
        if identity["provider_id"] is None:
            issues.append("DRAMA_UNMAPPED")
            values.update(provider_share=None, net_revenue=None, business_revenue=None)
        if mapping and mapping["drama_id"] is None:
            issues.append("DRAMA_FROM_REPORT_NAME")
        from app.modules.operation_logs.evidence import build_evidence

        auto = build_evidence(
            session,
            tenant_id=run.tenant_id,
            advertiser_id=f.advertiser_id,
            campaign_id=f.campaign_id,
            day=f.stat_date,
            timezone=f.timezone,
            frozen=run.inputs.get("operation_logs", {}),
            spend=f.spend,
            events=f.ad_impression_events,
            revenue=f.revenue_d0,
            cache=evidence_cache,
            report_revision=run.inputs.get("states", {})
            .get(f"{f.advertiser_id}/{f.stat_date}", {})
            .get("version"),
        )
        result = auto.data
        bid = Decimal(result["bid"]) if result["bid"] is not None else None
        compensation = (
            Decimal(result["amount"]) if result["amount"] is not None else None
        )
        eligible = False if result["status"] == "ZERO" else None
        source = f"official-operation-evidence:{auto.id}:{auto.digest}"
        manual = run.inputs.get("manual_evidence", {}).get(
            f"{f.advertiser_id}/{f.campaign_id}/{f.stat_date}"
        )
        if manual and manual.get("evidence_revision") == auto.digest:
            bid = Decimal(manual["bid"]) if manual["bid"] is not None else bid
            eligible = manual["eligible"]
            compensation = compensation_amount(
                f.spend, f.revenue_d0, bid, f.ad_impression_events, eligible
            )
            source = f"admin-correction:{auto.id}:{auto.digest}"
        elif manual:
            issues.append("COMPENSATION_MANUAL_CONFLICT")
        if compensation is None:
            issues.append("COMPENSATION_EVIDENCE_MISSING")
        elif result["status"] == "ESTIMATED_PENDING":
            issues.append("COMPENSATION_QUALIFICATION_PENDING")
        rows.append(
            dict(
                run_id=run.id,
                tenant_id=run.tenant_id,
                bc_id=run.bc_id,
                stat_date=f.stat_date,
                advertiser_id=f.advertiser_id,
                campaign_id=f.campaign_id,
                campaign_name=f.campaign_name or f.campaign_id,
                currency=f.currency,
                timezone=f.timezone,
                revenue_d0=f.revenue_d0,
                iaa_events=f.ad_impression_events,
                provider_rate=provider_rate,
                rebate_rate=rebate_rate,
                compensation_bid=bid,
                compensation_eligible=eligible,
                compensation_source=source,
                **identity,
                **values,
                compensation=compensation,
                issues=issues,
            )
        )
    if rows:
        # ORM批量插入默认忽略None，会把待配置返点错误恢复成模型默认0。
        typing_cast(SQLAlchemySession, session).execute(
            insert(BusinessDay), rows, execution_options={"render_nulls": True}
        )
        run.rows_count += len(rows)
    run.base_after = (
        [facts[-1].advertiser_id, facts[-1].campaign_id]
        if len(facts) == BASE_BATCH
        else None
    )
    if len(facts) < BASE_BATCH:
        run.cursor += timedelta(days=1)


def _subsidy_day(session: Session, run: BusinessRun) -> None:
    # 每个系列截至当日的季度权重；零消耗合成行不会增加门槛。
    B = BusinessDay
    records = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            sa_select(
                col(B.drama_key),
                col(B.advertiser_id),
                col(B.campaign_id),
                col(B.currency),
                func.sum(col(B.spend)),
                func.sum(col(B.spend)).filter(col(B.stat_date) < run.cursor),
                func.bool_or(col(B.spend).is_(None)),
                func.max(col(B.drama_name)),
                func.max(col(B.campaign_name)),
                func.max(col(B.provider_name)),
                func.max(col(B.timezone)),
                func.min(cast(col(B.provider_id), Text)),
                func.min(cast(col(B.drama_id), Text)),
            )
            .where(col(B.run_id) == run.id, col(B.stat_date) <= run.cursor)
            .group_by(
                col(B.drama_key),
                col(B.advertiser_id),
                col(B.campaign_id),
                col(B.currency),
            )
        )
        .all()
    )
    groups = defaultdict(list)
    series_identities = defaultdict(set)
    for r in records:
        groups[r[0]].append(r)
        series_identities[(r[1], r[2])].add(r[0])
    conflicting_series = {
        key for key, identities in series_identities.items() if len(identities) > 1
    }
    complete = _prefix_complete(run, run.cursor)
    existing = {
        (r.advertiser_id, r.campaign_id): r
        for r in session.exec(
            select(B).where(
                col(B.run_id) == run.id,
                col(B.stat_date) == run.cursor,
            )
        ).all()
    }
    uncertain = dict(run.coverage.get("subsidy_unknown", {}))
    uncertainty_changed = False
    # 身份无需重复跨表查询，沿用本版本中已冻结的系列元数据。
    for drama_key, series in groups.items():
        identity_conflict = any((r[1], r[2]) in conflicting_series for r in series)
        unknown = (
            identity_conflict
            or not complete
            or drama_key.startswith(("unmapped:", "named:"))
            or any(r[3] != "USD" or r[6] for r in series)
        )
        current = sum((r[4] or ZERO for r in series), ZERO)
        previous = sum((r[5] or ZERO for r in series), ZERO)
        cur = (None,) * 3 if unknown else tier_amounts(current, run.cursor, "USD")
        prev = (None,) * 3 if unknown else tier_amounts(previous, run.cursor, "USD")
        if unknown or cur[0] is None:
            if drama_key not in uncertain:
                uncertain[drama_key] = {
                    "from": run.cursor.isoformat(),
                    "providers": sorted({r[11] or "unmapped" for r in series}),
                    "reason": "DRAMA_IDENTITY_CONFLICT"
                    if identity_conflict
                    else "SUBSIDY_SCOPE_INCOMPLETE"
                    if unknown
                    else "SUBSIDY_POLICY_UNCONFIGURED",
                }
                uncertainty_changed = True
        weights = {f"{r[1]}/{r[2]}": r[4] or ZERO for r in series}
        allocations = [
            allocate_amount(a - b, weights) if a is not None and b is not None else None
            for a, b in zip(cur, prev, strict=True)
        ]
        for r in series:
            key = (r[1], r[2])
            row = existing.get(key)
            amounts = {
                name: parts.get(f"{r[1]}/{r[2]}", ZERO) if parts is not None else None
                for name, parts in zip(SUBSIDY_KEYS, allocations, strict=True)
            }
            # 只有补贴跨档时补历史系列当天0消费行，避免扩成系列×全部日期的笛卡尔积。
            if row is None and not any(v for v in amounts.values()):
                continue
            if row is None:
                zero_amounts: dict[str, Any] = dict.fromkeys(AMOUNT_KEYS, ZERO)
                row = BusinessDay(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    bc_id=run.bc_id,
                    advertiser_id=key[0],
                    campaign_id=key[1],
                    stat_date=run.cursor,
                    drama_key=drama_key,
                    drama_id=UUID(r[12]) if r[12] else None,
                    drama_name=r[7],
                    campaign_name=r[8],
                    provider_name=r[9],
                    timezone=r[10],
                    provider_id=UUID(r[11]) if r[11] else None,
                    currency=r[3],
                    **zero_amounts,
                )
                session.add(row)
            for name, amount in amounts.items():
                setattr(row, name, amount)
            if unknown:
                row.issues = list(
                    dict.fromkeys(
                        [
                            *row.issues,
                            "DRAMA_IDENTITY_CONFLICT"
                            if identity_conflict
                            else "SUBSIDY_SCOPE_INCOMPLETE",
                        ]
                    )
                )
            session.add(row)

    if uncertainty_changed:
        run.coverage = {**run.coverage, "subsidy_unknown": uncertain}


def _aggregate_day(session: Session, run: BusinessRun) -> None:
    """同日两份向上汇总在后台写好；保留账户维度以支持权限及账户筛选。"""
    session.flush()
    B = BusinessDay
    for kind in ("drama", "provider"):
        key = (
            col(B.drama_key)
            if kind == "drama"
            else func.coalesce(cast(col(B.provider_id), Text), literal("unmapped"))
        )
        identity = col(B.drama_id) if kind == "drama" else cast(literal(None), Uuid)
        name = col(B.drama_name) if kind == "drama" else col(B.provider_name)
        columns = [
            "run_id",
            "stat_date",
            "advertiser_id",
            "kind",
            "drama_key",
            "currency",
            "tenant_id",
            "bc_id",
            "drama_id",
            "provider_id",
            "drama_name",
            "provider_name",
            "campaign_name",
            "campaign_id",
            *AMOUNT_KEYS,
            "issues",
        ]
        statement = (
            sa_select(
                col(B.run_id),
                col(B.stat_date),
                col(B.advertiser_id),
                literal(kind),
                key,
                col(B.currency),
                col(B.tenant_id),
                col(B.bc_id),
                identity,
                col(B.provider_id),
                func.max(name),
                func.max(col(B.provider_name)),
                literal(""),
                literal(""),
                *[
                    func.sum(getattr(B, k))
                    if k == "compensation"
                    else case(
                        (func.count(getattr(B, k)) < func.count(), None),
                        else_=func.sum(getattr(B, k)),
                    )
                    for k in AMOUNT_KEYS
                ],
                func.jsonb_path_query_array(
                    func.jsonb_agg(col(B.issues)), text("'$[*][*]'::jsonpath")
                ),
            )
            .where(col(B.run_id) == run.id, col(B.stat_date) == run.cursor)
            .group_by(
                col(B.run_id),
                col(B.stat_date),
                col(B.advertiser_id),
                key,
                col(B.currency),
                col(B.tenant_id),
                col(B.bc_id),
                identity,
                col(B.provider_id),
            )
        )
        typing_cast(SQLAlchemySession, session).execute(
            insert(BusinessSummaryDay).from_select(columns, statement)
        )


def _freeze_candidate(
    session: Session, run: BusinessRun, scope: BusinessScope, now: datetime
) -> bool:
    # 多个Reporting fork只允许一个重型冻结；锁随事务退出，失败不会留Redis孤锁。
    if (
        not typing_cast(SQLAlchemySession, session)
        .execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SNAPSHOT_LOCK})
        .scalar_one()
    ):
        return False
    snapshot, signature = source_snapshot(
        session, scope, run.quarter, run.end_date, now
    )
    publication = session.get(
        BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)
    )
    previous = (
        session.exec(
            narrow_runs().where(col(BusinessRun.id) == publication.run_id)
        ).first()
        if publication
        else None
    )
    # 轻占位未带事实；未变输入只能退役，不能伪造COMPLETE后切换空发布。
    if (
        previous is not None
        and previous.status == "COMPLETE"
        and previous.source_signature == signature
        and previous.scope_revision == run.scope_revision
        and previous.end_date == run.end_date
    ):
        run.status = "SUPERSEDED"
        run.source_signature = signature
    else:
        run.inputs = {**snapshot, "dirty_start": run.quarter.isoformat(), "after": None}
        run.source_signature = signature
        run.status = "RUNNING"
        begin_prefix(session, run)
    # 补源仍沿原额度/队列合同，移到已有冻结输入的Worker，Control不反解大JSON。
    from app.modules.business.tasks import _request_missing_sources

    _request_missing_sources(session, scope, [run], now, snapshots={run.id: snapshot})
    session.add(run)
    session.flush()
    return run.status == "SUPERSEDED"


def _verify_candidate(
    session: Session,
    run: BusinessRun,
    scope: BusinessScope,
    now: datetime,
    allowed: list[str],
) -> bool:
    # 初次冻结与最终核验共用单例，不能让多个fork同时展开大型季度快照。
    if (
        not typing_cast(SQLAlchemySession, session)
        .execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SNAPSHOT_LOCK})
        .scalar_one()
    ):
        return False
    session.refresh(run, attribute_names=["inputs"])
    if not isinstance(run.inputs.get("accounts"), dict) or not set(
        run.inputs["accounts"]
    ) <= set(allowed):
        run.status = "SUPERSEDED"
        return True
    snapshot, signature = source_snapshot(
        session, scope, run.quarter, run.end_date, now
    )
    if signature != run.source_signature:
        dirty = rebase_dirty_start(run, snapshot)
        if dirty is None:
            run.status = "SUPERSEDED"
            return True
        if dirty <= run.end_date:
            begin_rebase(run, snapshot, signature, dirty)
            session.add(run)
            session.flush()
            return False
        # cutoff元数据变更但没有任何账户日覆盖资格变化，直接保存真实快照。
        run.inputs = {
            **snapshot,
            "dirty_start": run.inputs["dirty_start"],
            "after": None,
        }
        run.source_signature = signature
    missing = sorted(
        {
            d
            for a in run.inputs["accounts"]
            for d in _days(run.quarter, run.end_date)
            if not _source_complete(run, a, d)
        }
    )
    run.coverage = {
        **run.coverage,
        "missing_dates": [d.isoformat() for d in missing],
        "policy_restricted": run.inputs.get("policy_restricted", False),
    }
    run.status, run.finished_at = "COMPLETE", now
    statement = pg_insert(BusinessPublication).values(
        tenant_id=run.tenant_id,
        bc_id=run.bc_id,
        quarter=run.quarter,
        run_id=run.id,
    )
    typing_cast(SQLAlchemySession, session).execute(
        statement.on_conflict_do_update(
            index_elements=["tenant_id", "bc_id", "quarter"],
            set_={"run_id": run.id},
        )
    )
    from app.modules.business.publication import publish_ready_manifest

    publish_ready_manifest(session, scope)
    session.add(run)
    session.flush()
    return True


def advance_run(session: Session, run_id: UUID, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    identity = session.exec(narrow_runs().where(col(BusinessRun.id) == run_id)).first()
    if identity is None:
        return True
    # 与配置/规划一致的BC→运行锁顺序，避免发布与候选替换互相等待。
    parent = session.exec(
        select(TenantBC)
        .where(
            col(TenantBC.tenant_id) == identity.tenant_id,
            col(TenantBC.bc_id) == identity.bc_id,
        )
        .with_for_update(key_share=True, skip_locked=True)
    ).first()
    if parent is None:
        return False
    run = session.exec(
        narrow_runs()
        .where(col(BusinessRun.id) == run_id)
        .with_for_update(skip_locked=True)
    ).first()
    if run is None or run.status in {"COMPLETE", "SUPERSEDED", "FAILED"}:
        return True
    scope = session.exec(
        select(BusinessScope)
        .where(
            col(BusinessScope.tenant_id) == run.tenant_id,
            col(BusinessScope.bc_id) == run.bc_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        scope is None
        or scope.revision != run.scope_revision
        or scope.actor_id != run.actor_id
        or (
            run.phase != "SNAPSHOT"
            and frozen_metadata(session, run.id).get("calculation_revision") != 4
        )
    ):
        run.status = "SUPERSEDED"
        session.add(run)
        return True
    context = TenantContext(
        tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
    )
    allowed = scope_accounts(session, context, run.bc_id)
    if run.phase not in KNOWN_PHASES:
        raise ValueError("未知经营核算阶段")
    if run.phase == "SNAPSHOT":
        return _freeze_candidate(session, run, scope, now)
    if run.phase == "VERIFY":
        return _verify_candidate(session, run, scope, now, allowed)
    if not frozen_accounts(session, run.id) <= set(allowed):
        run.status = "SUPERSEDED"
        session.add(run)
        return True
    run.status = "RUNNING"
    if run.phase == "REBASE":
        advance_rebase(session, run, batch=BASE_BATCH)
        session.add(run)
        session.flush()
        return False
    if run.phase in PREPARATION_PHASES:
        advance_prefix(session, run, batch=BASE_BATCH)
        session.add(run)
        session.flush()
        return run.status == "SUPERSEDED"
    # 只有原有计算阶段需要完整冻结输入；准备和复制阶段禁止隐式加载。
    session.refresh(run, attribute_names=["inputs"])
    if run.phase == "BASE":
        if run.cursor <= run.end_date:
            _base_page(session, run)
        else:
            run.phase, run.cursor = (
                "SUBSIDY",
                date.fromisoformat(run.inputs["dirty_start"]),
            )
    elif run.phase == "SUBSIDY" and run.cursor <= run.end_date:
        _subsidy_day(session, run)
        _aggregate_day(session, run)
        run.cursor += timedelta(days=1)
    else:
        # 最终重型来源核验独立占一个18秒工作单元，不挤入剩余3秒量子。
        run.phase = "VERIFY"
    session.add(run)
    session.flush()
    return run.status in {"COMPLETE", "SUPERSEDED", "FAILED"}


def _days(start: date, end: date) -> Iterator[date]:
    while start <= end:
        yield start
        start += timedelta(days=1)
