"""经营核算复用现有Worker；Beat只唤醒，持久任务恢复不依赖一次消息成功。"""

import logging
from datetime import UTC, date, datetime, timedelta
from time import monotonic

from fastapi import HTTPException
from sqlalchemy import and_
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.modules.accounts.models import TenantBC
from app.modules.business.calculation import quarter_start
from app.modules.business.configuration import scope_accounts
from app.modules.business.models import BusinessRun, BusinessScope
from app.modules.business.projection import (
    _source_complete,
    advance_run,
    next_quarter,
    request_rebuild,
)
from app.modules.reporting.store_models import AdSyncBc

logger = logging.getLogger(__name__)


def schedule_due(session: Session, *, now: datetime, limit: int = 2) -> int:
    scopes = session.exec(
        select(BusinessScope)
        .where(col(BusinessScope.next_check_at) <= now)
        .order_by(col(BusinessScope.next_check_at))
        .limit(limit)
    ).all()
    count = 0
    for scope in scopes:
        context = TenantContext(
            tenant_id=scope.tenant_id, actor_id=scope.actor_id, role="operator"
        )
        try:
            allowed = scope_accounts(session, context, scope.bc_id)
        except DomainError, HTTPException:
            scope.next_check_at = now + timedelta(hours=1)
            session.add(scope)
            continue
        if not allowed:
            scope.next_check_at = now + timedelta(hours=1)
            session.add(scope)
            continue
        # 只核算完整日。短周期发现来源修正，签名相同就复用已有版本，不重复计算。
        recent = request_rebuild(
            session,
            context=context,
            bc_id=scope.bc_id,
            start_date=max(scope.start_date, now.date() - timedelta(days=32)),
            end_date=now.date() + timedelta(days=1),
            now=now,
        )
        if recent and all(
            r.status == "COMPLETE" and not r.coverage.get("missing_dates")
            for r in recent
        ):
            scope.last_daily = now.date()
        count += len(recent)
        # 更早历史按季度轮转，底层按系列页/日处理，不阻塞最近区间先可查询。
        quarter = scope.history_cursor or quarter_start(scope.start_date)
        if quarter >= quarter_start(now.date() - timedelta(days=32)):
            quarter = quarter_start(scope.start_date)
        historical = request_rebuild(
            session,
            context=context,
            bc_id=scope.bc_id,
            start_date=quarter,
            end_date=min(
                next_quarter(quarter) - timedelta(days=1),
                now.date() + timedelta(days=1),
            ),
            now=now,
        )
        count += len(historical)
        scope.history_cursor = next_quarter(quarter)
        scope.next_check_at = now + timedelta(minutes=5)
        session.add(scope)
        _request_missing_sources(session, scope, [*recent, *historical], now)
    return count


def missing_source_windows(
    run: BusinessRun, owned: set[str]
) -> list[tuple[str, date, date]]:
    windows = []
    for advertiser, meta in run.inputs["accounts"].items():
        if advertiser not in owned:
            continue
        start = max(run.quarter, date.fromisoformat(meta["start"]))
        end = min(
            run.end_date,
            date.fromisoformat(meta["complete_through"]),
            date.fromisoformat(meta["end"]) if meta["end"] else run.end_date,
        )
        while start <= end:
            if not _source_complete(run, advertiser, start):
                windows.append(
                    (advertiser, start, min(start + timedelta(days=29), end))
                )
                break
            start += timedelta(days=1)
    return windows


def _request_missing_sources(
    session: Session, scope: BusinessScope, runs: list[BusinessRun], now: datetime
) -> int:
    """只补必要ad_day，沿现有SDK/额度合同，不顺带建立素材任务。"""
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.planner import _common, owned_accounts

    plan = session.get(AdSyncBc, (scope.tenant_id, scope.bc_id))
    if not settings.ADS_SYNC_ENABLED or plan is None or not plan.enabled:
        return 0
    owned = set(owned_accounts(session, plan))
    windows = sorted(
        {window for run in runs for window in missing_source_windows(run, owned)}
    )
    keyed = [
        (f"{advertiser}/{start}/{end}", advertiser, start, end)
        for advertiser, start, end in windows
    ]
    # 轮转补源，避免前两个尚未完成的账户长期占住补源入口。
    if scope.repair_cursor:
        keyed = [w for w in keyed if w[0] > scope.repair_cursor] + [
            w for w in keyed if w[0] <= scope.repair_cursor
        ]
    for key, advertiser, start, end in keyed[:2]:
        enqueue_job(
            session,
            **_common(plan),
            kind="ad_day",
            tier="history",
            advertiser_ids=[advertiser],
            start=start,
            end=end,
            requested_at=now,
        )
        scope.repair_cursor = key
    session.add(scope)
    return min(2, len(keyed))


@celery_app.task(name="business.schedule", soft_time_limit=25, time_limit=30)  # type: ignore[untyped-decorator]
def schedule_business() -> int:
    from app.modules.business.maintenance import cleanup_business

    with Session(engine) as session:
        count = schedule_due(session, now=datetime.now(UTC))
        session.commit()
        cleanup_business(session, now=datetime.now(UTC))
        session.commit()
        return count


@celery_app.task(name="business.work", soft_time_limit=25, time_limit=30)  # type: ignore[untyped-decorator]
def work_business() -> int:
    completed, deadline = 0, monotonic() + 3
    while monotonic() < deadline:
        run_id = None
        try:
            with Session(engine) as session:
                run_id = session.exec(
                    select(col(BusinessRun.id))
                    .join(
                        TenantBC,
                        and_(
                            col(TenantBC.tenant_id) == col(BusinessRun.tenant_id),
                            col(TenantBC.bc_id) == col(BusinessRun.bc_id),
                        ),
                    )
                    .where(
                        col(BusinessRun.status).in_(["QUEUED", "RUNNING"]),
                    )
                    .order_by(
                        col(BusinessRun.quarter).desc(), col(BusinessRun.created_at)
                    )
                    .limit(1)
                    .with_for_update(of=TenantBC, skip_locked=True)
                ).first()
                if run_id is None:
                    break
                done = advance_run(session, run_id)
                session.commit()
                completed += int(done)
        except Exception:
            # 不保存原始外部返回/凭据；每个失败版本独立，旧发布结果仍可查询。
            logger.exception(
                "business calculation failed", extra={"business_run_id": str(run_id)}
            )
            if run_id:
                with Session(engine) as session:
                    run = session.get(BusinessRun, run_id)
                    if run and run.status not in {"COMPLETE", "SUPERSEDED"}:
                        run.status, run.error = "FAILED", "经营核算失败，请重试"
                        session.add(run)
                        session.commit()
            break
    from app.modules.business.exports import process_pending_export

    with Session(engine) as session:
        process_pending_export(session)
        session.commit()
    return completed
