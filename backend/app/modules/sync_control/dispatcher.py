"""以到期调度头选择工作；只在原平台账本可运行时领取及写outbox。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast as typing_cast
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import and_, case, false, func, or_, text, union_all, update
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.jobs.admission import admission_policy
from app.modules.reporting.store_models import (
    ReportSyncJob,
)
from app.modules.sync_control.costs import (
    cost_classes,
    reserve_execution_cost,
)
from app.modules.sync_control.dispatch_budget import (
    DispatchBudgetConflict,
    EndpointBudgets,
    dispatch_budget_transaction,
)
from app.modules.sync_control.dispatch_envelope import enqueue_sync_dispatch
from app.modules.sync_control.fairness import WEIGHTS, choose_work_class
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
    SyncWorkerPool,
)
from app.modules.sync_control.occupancy import release_dispatch, reserve_dispatch
from app.modules.sync_control.requirements import execution_owner_current
from app.modules.sync_control.schedule import (
    operation_log_order,
    pending_execution_dues,
    report_endpoint,
)
from app.modules.sync_control.worker_pool import (
    background_lease_count,
    report_background_category,
    reserve_background_turn,
)


def _pool_ready_heads(
    *, live_remaining: int, background_remaining: int, now: datetime
) -> list[ColumnElement[bool]]:
    del now
    # 所属池已满不算租户获得了机会；发现和领取共用容量条件。
    if not live_remaining and not background_remaining:
        return [false()]
    predicates: list[ColumnElement[bool]] = []
    if not live_remaining:
        predicates.append(col(SyncSchedule.work_class) != "realtime")
    if not background_remaining:
        predicates.append(col(SyncSchedule.work_class) != "history")
    return predicates


def _lock_execution_heads(
    session: Session, job: ReportSyncJob, head: SyncSchedule, *, now: datetime
) -> list[SyncSchedule] | None:
    """发现候选时不持锁；按统一顺序非阻塞取得整组，拿不齐即释放本次锁。"""
    statement = select(SyncSchedule).where(
        col(SyncSchedule.scope_id).in_(
            select(col(SyncExecutionScope.scope_id)).where(
                col(SyncExecutionScope.report_job_id) == job.id
            )
        ),
        col(SyncSchedule.work_class) == head.work_class,
    )
    with session.begin_nested() as attempt:
        expected = set(
            typing_cast(SQLAlchemySession, session)
            .execute(statement.with_only_columns(col(SyncSchedule.scope_id)))
            .scalars()
        )
        heads = session.exec(
            statement.order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).all()
        if (
            {row.scope_id for row in heads} != expected
            or head.scope_id not in expected
            or not head.active
            or head.next_eligible_at > now
            or (head.leased_until is not None and head.leased_until > now)
        ):
            attempt.rollback()
            return None
        return list(heads)


def dispatch_due(
    session: Session,
    redis: Redis,
    *,
    now: datetime,
    physical_slots: int,
    allow_backfill: bool = True,
    max_claims: int | None = None,
) -> int:
    try:
        with dispatch_budget_transaction(session, redis) as budgets:
            return _dispatch_due(
                session,
                redis,
                now=now,
                physical_slots=physical_slots,
                allow_backfill=allow_backfill,
                max_claims=max_claims,
                budgets=budgets,
            )
    except DispatchBudgetConflict:
        return 0


def _dispatch_due(
    session: Session,
    redis: Redis,
    *,
    now: datetime,
    physical_slots: int,
    allow_backfill: bool = True,
    max_claims: int | None = None,
    budgets: EndpointBudgets,
) -> int:
    # 一个租户轮次的cursor与租约/outbox同事务；第二Control不读改同一公平余额。
    # 家族窗口独立串行；共享后台池仅在实际后台准入时非阻塞锁定。
    if (
        not session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:sync-v2-dispatch',0))"
            )
        )
        .scalar_one()
    ):
        return 0
    active_jobs = list(
        session.exec(
            select(col(ReportSyncJob.id), col(ReportSyncJob.execution_pool)).where(
                text("report_sync_job.status = 'RUNNING'"),
                col(ReportSyncJob.claimed_until) > now,
            )
        ).all()
    )
    active_job_ids = [identity for identity, _ in active_jobs]
    live_remaining = max(
        0, physical_slots - sum(pool != "background" for _, pool in active_jobs)
    )
    pool = session.get(SyncWorkerPool, "background")
    background_remaining = max(
        0,
        (pool.capacity + pool.prefetch_slots if pool else 4)
        - background_lease_count(session, now=now),
    )
    # physical_slots同时限制单轮工作量和实时报告窗口；后台独立使用共享池，
    # 不能由36个实时报告把空闲的4个后台进程一起封死。
    available = max(0, min(physical_slots, live_remaining + background_remaining))
    if not available:
        return 0
    # 单次提交数量与物理窗口分开，避免后续领取超时撤销本次已经准备好的任务。
    claim_limit = available
    if max_claims is not None:
        if max_claims < 1:
            raise ValueError("max_claims must be positive")
        claim_limit = min(available, max_claims)
    dispatched = 0
    endpoints: Sequence[str] = tuple(
        dict.fromkeys(
            report_endpoint(kind)
            for kind in ("ad_day", "creative_day", "operation_log")
        )
    ) + tuple(
        report_endpoint("operation_log", {"operation_log": {"phase": phase}})
        for phase in ("CHECK", "DOWNLOAD")
    )
    # Worker槽不足时跨物理接口轮转；某接口提额不能永远占满全部槽。
    endpoints = session.exec(
        select(col(SyncSchedule.physical_endpoint))
        .where(
            col(SyncSchedule.active).is_(True),
            col(SyncSchedule.physical_endpoint).in_(endpoints),
        )
        .group_by(col(SyncSchedule.physical_endpoint))
        .order_by(
            func.max(col(SyncSchedule.last_dispatched_at)).asc().nullsfirst(),
            col(SyncSchedule.physical_endpoint),
        )
    ).all()
    for endpoint in endpoints:
        if dispatched >= claim_limit:
            break
        policy = admission_policy(
            "reports.integrated", channel="OFFICIAL_API", physical_endpoint=endpoint
        )
        due = [
            col(SyncSchedule.active).is_(True),
            col(SyncSchedule.physical_endpoint) == endpoint,
            col(SyncSchedule.next_eligible_at) <= now,
            or_(
                col(SyncSchedule.leased_until).is_(None),
                col(SyncSchedule.leased_until) <= now,
            ),
        ]
        if not allow_backfill:
            due.append(col(SyncSchedule.work_class) != "history")
        due.extend(
            _pool_ready_heads(
                live_remaining=live_remaining,
                background_remaining=background_remaining,
                now=now,
            )
        )
        # 有界租户批次；只读取到期头及其cursor，不排序全部执行历史。
        from app.modules.sync_control.fairness import due_tenants

        tenants = due_tenants(session, due, limit=min(64, available - dispatched))
        # 每轮各租户先给一页；槽多于租户时在下方有界循环公平借用。
        # 只读发现不提前锁head；被锁执行需要跳过候选后继续填槽，最多多探16个。
        skipped_scopes: set[UUID] = set()
        corrected = 0
        correction_limit = min(claim_limit, 4)
        for _round in range(claim_limit - dispatched + 16):
            if corrected >= correction_limit:
                break
            progressed = False
            for tenant_id in tenants:
                if dispatched >= claim_limit or not (
                    live_remaining or background_remaining
                ):
                    break
                cursor = budgets.acquire(tenant_id, endpoint)
                if cursor is None:
                    continue
                candidate_classes = [
                    work_class
                    for work_class in (
                        "realtime",
                        "history",
                        "directory",
                        "maintenance",
                    )
                    if not (
                        (work_class == "realtime" and not live_remaining)
                        or (work_class == "history" and not background_remaining)
                    )
                ]
                # 四个类仍各做索引 LIMIT 1，只合成一次往返；不能用 DISTINCT ON
                # 扫描/排序全部到期头，也不缓存候选或提前加锁。
                candidates = union_all(
                    *(
                        select(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
                        .where(
                            *due,
                            *_pool_ready_heads(
                                live_remaining=live_remaining,
                                background_remaining=background_remaining,
                                now=now,
                            ),
                            col(SyncSchedule.tenant_id) == tenant_id,
                            col(SyncSchedule.work_class) == work_class,
                            col(SyncSchedule.scope_id).not_in(skipped_scopes),
                        )
                        .order_by(
                            col(SyncSchedule.last_dispatched_at).asc().nullsfirst(),
                            col(SyncSchedule.oldest_pending_at).asc().nullsfirst(),
                        )
                        .limit(1)
                        for work_class in candidate_classes
                    )
                ).subquery()
                heads = list(
                    session.exec(
                        select(SyncSchedule).join(
                            candidates,
                            and_(
                                col(SyncSchedule.scope_id) == candidates.c.scope_id,
                                col(SyncSchedule.work_class) == candidates.c.work_class,
                            ),
                        )
                    ).all()
                )
                # 保持原工作类平局顺序；领取时仍按原路径短锁并刷新整组头。
                heads.sort(key=lambda row: candidate_classes.index(row.work_class))
                if not heads:
                    continue
                # 同批前一租户可能刚填满某池；仅有当前可用池的候选才算获得机会。
                # 其后的锁/配额阻塞仍轮转，避免受阻租户长期占住发现入口。
                previous_dispatched_at = cursor.last_dispatched_at
                cursor.last_dispatched_at = max(cursor.last_dispatched_at, now)
                retained, inflight_classes = cost_classes(
                    session, tenant_id=tenant_id, endpoint=endpoint, now=now
                )
                accounting_classes = {
                    head.work_class for head in heads
                } | inflight_classes
                work_class, state = choose_work_class(
                    cursor.fairness_state.get(endpoint, {}),
                    [head.work_class for head in heads],
                    retained=retained,
                    accounting=accounting_classes,
                )
                head = next(row for row in heads if row.work_class == work_class)
                low_budget = []
                busy_child = aliased(SyncExecutionScope)
                # 在途执行ID受物理窗口约束；空集合直接短路，不能让优化器
                # 为每个候选重扫全部lease/child表。不同日桶仍可并行。
                busy_requirements = (
                    select(col(SyncExecutionScope.requirement_id))
                    .join(
                        SyncScope, col(SyncScope.id) == col(SyncExecutionScope.scope_id)
                    )
                    .where(
                        col(SyncExecutionScope.report_job_id).in_(active_job_ids),
                        col(SyncExecutionScope.owner_epoch)
                        == col(SyncScope.owner_epoch),
                        col(SyncExecutionScope.status) == "PENDING",
                    )
                    .cte("busy_report_requirements")
                    .prefix_with("MATERIALIZED")
                )
                execution_busy = (
                    select(col(busy_child.id))
                    .join(
                        busy_requirements,
                        busy_requirements.c.requirement_id
                        == col(busy_child.requirement_id),
                    )
                    .where(col(busy_child.report_job_id) == col(ReportSyncJob.id))
                    .correlate(ReportSyncJob)
                    .exists()
                )
                job = session.exec(
                    select(ReportSyncJob)
                    .join(
                        SyncExecutionScope,
                        col(SyncExecutionScope.report_job_id) == col(ReportSyncJob.id),
                    )
                    .join(
                        SyncRequirement,
                        col(SyncRequirement.id)
                        == col(SyncExecutionScope.requirement_id),
                    )
                    .join(
                        SyncScope, col(SyncScope.id) == col(SyncExecutionScope.scope_id)
                    )
                    .where(
                        col(SyncExecutionScope.scope_id) == head.scope_id,
                        col(SyncExecutionScope.owner_epoch)
                        == col(SyncScope.owner_epoch),
                        col(ReportSyncJob.owner_epoch) == col(SyncScope.owner_epoch),
                        execution_owner_current(ReportSyncJob, "report_job_id"),
                        col(SyncExecutionScope.status) == "PENDING",
                        col(SyncRequirement.source) == head.work_class,
                        col(ReportSyncJob.status).in_(["QUEUED", "WAITING", "RUNNING"]),
                        col(ReportSyncJob.due_at) <= now,
                        *low_budget,
                        ~execution_busy if active_job_ids else True,
                        or_(
                            col(ReportSyncJob.claimed_until).is_(None),
                            col(ReportSyncJob.claimed_until) <= now,
                        ),
                    )
                    .order_by(
                        # 需求晋升不把旧宽日期执行伪装成新实时执行；先派冻结了当前revision的后继。
                        case(
                            (
                                col(SyncExecutionScope.frozen_revision)
                                == col(SyncRequirement.desired_revision),
                                0,
                            ),
                            else_=1,
                        ),
                        *operation_log_order(),
                        col(ReportSyncJob.created_at),
                        col(ReportSyncJob.id),
                    )
                    .limit(1)
                    .with_for_update(of=ReportSyncJob, skip_locked=True)
                ).first()
                if job is None:
                    # 单头修正也只在拿到锁并刷新之后做，不能以发现时的旧状态覆盖outcome。
                    locked_head = session.exec(
                        select(SyncSchedule)
                        .where(
                            col(SyncSchedule.scope_id) == head.scope_id,
                            col(SyncSchedule.work_class) == head.work_class,
                        )
                        .with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True)
                    ).one_or_none()
                    if locked_head is None or (
                        locked_head.leased_until is not None
                        and locked_head.leased_until > now
                    ):
                        skipped_scopes.add(head.scope_id)
                        progressed = True
                        continue
                    head = locked_head
                    # 旧due头不等于执行已就绪：按真实执行due/有效worker租约纠正，
                    # 避免每秒重查同一未来等待前缀，持公共锁却派不出请求。
                    dues = pending_execution_dues(
                        session, [head], "report", now=now, respect_claims=True
                    )
                    next_execution_due = dues.get((head.scope_id, head.work_class))
                    head.last_dispatched_at = now
                    if (
                        low_budget
                        and next_execution_due is not None
                        and next_execution_due <= now
                    ):
                        # 原执行真实已到期，只是池/低预算筛选暂不可运行：保持due，
                        # 但持久轮转服务年龄，不能每次又被同一前缀挡住后方就绪任务。
                        skipped_scopes.add(head.scope_id)
                        progressed = True
                        continue
                    head.next_eligible_at = max(
                        now + timedelta(seconds=1),
                        next_execution_due or now + timedelta(seconds=5),
                    )
                    head.active = (
                        session.exec(
                            select(col(SyncRequirement.id))
                            .where(
                                col(SyncRequirement.scope_id) == head.scope_id,
                                col(SyncRequirement.source) == head.work_class,
                                col(SyncRequirement.satisfied_revision)
                                < col(SyncRequirement.desired_revision),
                            )
                            .limit(1)
                        ).first()
                        is not None
                    )
                    progressed = True
                    continue
                execution_heads = _lock_execution_heads(session, job, head, now=now)
                if execution_heads is None:
                    skipped_scopes.add(head.scope_id)
                    progressed = True
                    continue
                actual_endpoint = report_endpoint(job.kind, job.progress)
                if actual_endpoint != endpoint:
                    # 旧CREATE头/同scope混阶段仅修正路由；保持job→整组head锁序。
                    # 当前接口的cursor不能为另一接口领取、计费或预占Redis。
                    for sibling in execution_heads:
                        sibling.physical_endpoint = actual_endpoint
                        sibling.wait_reason = None
                    cursor.last_dispatched_at = previous_dispatched_at
                    skipped_scopes.add(head.scope_id)
                    progressed = True
                    # 纠正也消耗本轮查询时间；小批提交旧头迁移，不能无领取地
                    # 扫到整事务超时回滚。只让出当前接口，其他报表接口仍有机会。
                    corrected += 1
                    if corrected >= correction_limit:
                        break
                    continue
                token = uuid4()
                ready = reserve_dispatch(
                    redis,
                    reservation_id=token,
                    policy=policy,
                    app_scope=settings.TIKTOK_APP_ID,
                    endpoint=endpoint,
                    rate_group=endpoint,
                    tenant_id=tenant_id,
                    advertiser_id=job.advertiser_ids[0],
                )
                if ready.capacity_hint == 0:
                    head.next_eligible_at = now + timedelta(
                        milliseconds=max(1, ready.retry_after_ms)
                    )
                    head.wait_reason = ready.reason
                    continue
                assert work_class is not None
                category = report_background_category(
                    session, job, work_class=work_class, now=now
                )
                if category is not None and not reserve_background_turn(
                    session,
                    redis,
                    category=category,
                    now=now,
                    allow_backfill=allow_backfill,
                ):
                    release_dispatch(
                        redis,
                        reservation_id=token,
                        app_scope=settings.TIKTOK_APP_ID,
                        endpoint=endpoint,
                        rate_group=endpoint,
                        tenant_id=tenant_id,
                        advertiser_id=job.advertiser_ids[0],
                    )
                    # 平台可调用而共享池未给轮次时，本次只继续填独立实时槽。
                    # 不在同一公平状态上反复选中历史任务，也不跨池扩大后台窗口；
                    # 下一次调度仍按原共享池竞争后台，当前日关联沿low_budget保留。
                    background_remaining = 0
                    progressed = True
                    continue
                job.execution_pool = (
                    "background" if category is not None else "live-report"
                )
                job.claim_token, job.claimed_until = token, now + timedelta(seconds=240)
                job.claim_generation += 1
                # 领取即冻结子引用代数；发送前撤权也必须能结束这一代，不能留下悬空PENDING。
                session.connection().execute(
                    update(SyncExecutionScope)
                    .where(
                        col(SyncExecutionScope.report_job_id) == job.id,
                        col(SyncExecutionScope.status) == "PENDING",
                        col(SyncExecutionScope.owner_epoch) == job.owner_epoch,
                    )
                    .values(execution_generation=job.claim_generation)
                )
                job.status = "RUNNING"
                head.lease_token, head.leased_until = token, job.claimed_until
                head.lease_execution_id, head.lease_execution_generation = (
                    job.id,
                    job.claim_generation,
                )
                head.generation += 1
                head.wait_reason, head.last_dispatched_at = None, now
                # 一个BASIC执行冻结的全部账户头一起占位，排队期间不能从另一子scope重复派发。
                for sibling in execution_heads:
                    sibling.lease_token, sibling.leased_until = token, job.claimed_until
                    sibling.lease_execution_id = job.id
                    sibling.lease_execution_generation = job.claim_generation
                    sibling.last_dispatched_at, sibling.wait_reason = now, None
                cursor.last_dispatched_at = now
                expected = 1
                assert work_class is not None
                reserve_execution_cost(
                    session,
                    cursor=cursor,
                    execution=job,
                    kind="report",
                    endpoint=endpoint,
                    work_class=work_class,
                    eligible_weights={key: WEIGHTS[key] for key in accounting_classes},
                    state_after_unit_charge=state,
                    reserved_units=expected,
                    lease_until=job.claimed_until,
                )
                enqueue_sync_dispatch(
                    session,
                    execution=job,
                    now=now,
                    context=TenantContext(
                        tenant_id=tenant_id, actor_id=job.actor_id, role="operator"
                    ),
                    task_name="reporting.backfill_step"
                    if category is not None
                    else "reporting.job_step",
                    task_key=f"report-job:{job.id}:{token}",
                    payload={"job_id": str(job.id), "claim_id": str(token)},
                )
                active_job_ids.append(job.id)
                if category is None:
                    live_remaining -= 1
                else:
                    background_remaining -= 1
                dispatched += 1
                progressed = True
            if not progressed:
                break
    return dispatched
