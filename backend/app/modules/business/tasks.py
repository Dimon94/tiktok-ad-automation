"""经营核算复用现有Worker；Beat只唤醒，持久任务恢复不依赖一次消息成功。"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy.exc import DBAPIError
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
from app.modules.business.prefix import (
    HEAVY_PHASES,
    PREPARATION_PHASES,
    narrow_runs,
    repair_windows,
)
from app.modules.business.projection import (
    _source_complete,
    advance_run,
    next_quarter,
    request_rebuild,
)
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
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
            defer_snapshot=True,
            coalesce_pending=True,
        )
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
            defer_snapshot=True,
            coalesce_pending=True,
        )
        count += len(historical)
        scope.history_cursor = next_quarter(quarter)
        scope.next_check_at = now + timedelta(minutes=5)
        session.add(scope)
        # 长准备/复制期间继续轮转补源，只让PG返回小窗口；取消不能撤销轻调度。
        try:
            with session.begin_nested(), query_budget(session, seconds=2):
                _request_missing_sources(session, scope, [*recent, *historical], now)
        except (QueryBudgetExceeded, DBAPIError) as error:
            if (
                isinstance(error, DBAPIError)
                and getattr(error.orig, "sqlstate", None) != "57014"
            ):
                raise
    return count


def missing_source_windows(
    run: BusinessRun, owned: set[str], *, inputs: dict[str, Any] | None = None
) -> list[tuple[str, date, date]]:
    windows = []
    # SNAPSHOT尚未变更时不保存第二份大JSON，补源直接使用本次已读取的值。
    if inputs is not None:
        run = BusinessRun(
            tenant_id=run.tenant_id,
            bc_id=run.bc_id,
            actor_id=run.actor_id,
            quarter=run.quarter,
            end_date=run.end_date,
            scope_revision=run.scope_revision,
            cursor=run.cursor,
            inputs=inputs,
        )
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
    session: Session,
    scope: BusinessScope,
    runs: list[BusinessRun],
    now: datetime,
    *,
    snapshots: dict[UUID, dict[str, Any]] | None = None,
) -> int:
    """只补必要ad_day，沿现有SDK/额度合同，不顺带建立素材任务。"""
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.planner import _common, owned_accounts

    plan = session.get(AdSyncBc, (scope.tenant_id, scope.bc_id))
    if not settings.ADS_SYNC_ENABLED or plan is None or not plan.enabled:
        return 0
    owned = set(owned_accounts(session, plan))
    windows_set: set[tuple[str, date, date]] = set()
    scanned: list[str] = []
    for run in {r.id: r for r in runs}.values():
        if snapshots is not None and run.id in snapshots:
            windows_set.update(
                missing_source_windows(run, owned, inputs=snapshots[run.id])
            )
        elif run.phase != "SNAPSHOT":
            windows, after = repair_windows(session, run, owned, scope.repair_cursor)
            windows_set.update(windows)
            if after is not None:
                scanned.append(after)
    windows = sorted(windows_set)
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
    if not keyed and scanned:
        # 连续完整账户也推进扫描游标；不能永远扫描同一128账号前缀。
        scope.repair_cursor = f"{max(scanned)}/9999-12-31/9999-12-31"
    session.add(scope)
    return min(2, len(keyed))


@celery_app.task(name="business.schedule", soft_time_limit=25, time_limit=30)  # type: ignore[untyped-decorator]
def schedule_business() -> int:
    from app.modules.business.maintenance import cleanup_business

    with Session(engine) as session:
        try:
            # 单个25秒唤醒只处理一个BC，先提交其进度；不能让第二个大快照
            # 耗尽期限后把前一个BC的调度一起撤销，后续BC按原到期顺序轮转。
            count = schedule_due(session, now=datetime.now(UTC), limit=1)
            session.commit()
            cleanup_business(session, now=datetime.now(UTC))
            session.commit()
            return count
        except SoftTimeLimitExceeded:
            # 软中断可能落在驱动解码/接收结果中；直接丢弃物理连接并释放事务锁，
            # 不能把仍有协议结果的连接放回Control池污染后续同步任务。
            session.invalidate()
            raise


@contextmanager
def _work_session() -> Iterator[Session]:
    with Session(engine) as session:
        try:
            yield session
        except SoftTimeLimitExceeded:
            # 信号可打断驱动收包，先丢弃物理连接，不能在退出时复用坏连接。
            session.invalidate()
            raise


@celery_app.task(name="business.work", soft_time_limit=25, time_limit=30)  # type: ignore[untyped-decorator]
def work_business() -> int:
    completed, deadline = 0, monotonic() + 3
    heavy_attempted = False
    while monotonic() < deadline:
        run_id = None
        phase = None
        try:
            with _work_session() as session:
                chosen = session.exec(
                    select(col(BusinessRun.id), col(BusinessRun.phase))
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
                    # 经营互斥不锁BC主键，避免阻塞同步任务的外键校验。
                    .with_for_update(of=TenantBC, key_share=True, skip_locked=True)
                ).first()
                if chosen is None:
                    break
                run_id, phase = chosen
                heavy_attempted = phase in HEAVY_PHASES
                if phase in PREPARATION_PHASES:
                    # 重型冻结单独占一个有界工作单元，普通准备/复制共享本轮3秒。
                    seconds = (
                        18.0
                        if phase in HEAVY_PHASES
                        else max(0.0, deadline - monotonic())
                    )
                    with query_budget(session, seconds=seconds):
                        done = advance_run(session, run_id)
                else:
                    done = advance_run(session, run_id)
                session.commit()
                completed += int(done)
                if phase in HEAVY_PHASES:
                    break
        except SoftTimeLimitExceeded:
            raise
        except Exception as error:
            if phase in PREPARATION_PHASES and (
                isinstance(error, QueryBudgetExceeded)
                or isinstance(error, DBAPIError)
                and getattr(error.orig, "sqlstate", None) == "57014"
            ):
                # 有界工作尚未提交，下一次从持久游标继续，不冒充业务失败。
                break
            # 不保存原始外部返回/凭据；每个失败版本独立，旧发布结果仍可查询。
            logger.exception(
                "business calculation failed", extra={"business_run_id": str(run_id)}
            )
            if run_id:
                with Session(engine) as session:
                    run = session.exec(
                        narrow_runs().where(col(BusinessRun.id) == run_id)
                    ).first()
                    if run and run.status not in {"COMPLETE", "SUPERSEDED"}:
                        run.status, run.error = "FAILED", "经营核算失败，请重试"
                        session.add(run)
                        session.commit()
            break
    if heavy_attempted:
        return completed
    from app.modules.business.exports import process_pending_export

    with Session(engine) as session:
        process_pending_export(session)
        session.commit()
    return completed
