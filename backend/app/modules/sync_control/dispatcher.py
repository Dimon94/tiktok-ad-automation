"""以到期调度头选择工作；只在原平台账本可运行时领取及写outbox。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import and_, case, func, or_, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.jobs.admission import admission_policy
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.store_models import (
    ReportDispatchCursor,
    ReportSyncJob,
)
from app.modules.sync_control.costs import (
    cost_classes,
    reserve_execution_cost,
    settle_costs,
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
from app.modules.sync_control.schedule import report_endpoint
from app.modules.sync_control.worker_pool import (
    background_lease_count,
    lock_background_pool,
    report_background_category,
    reserve_background_turn,
)


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
            session.execute(
                statement.with_only_columns(col(SyncSchedule.scope_id))
            ).scalars()
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
) -> int:
    if not lock_background_pool(session):
        return 0
    # 一个租户轮次的cursor与租约/outbox同事务；第二Control不读改同一公平余额。
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
    dispatched = 0
    endpoints: Sequence[str] = tuple(
        dict.fromkeys(
            report_endpoint(kind)
            for kind in ("ad_day", "material_day", "material_links")
        )
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
        if dispatched >= available:
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
        # 有界租户批次；只读取到期头及其cursor，不排序全部执行历史。
        tenants = session.exec(
            select(
                col(SyncSchedule.tenant_id),
                col(ReportDispatchCursor.last_dispatched_at),
            )
            .outerjoin(
                ReportDispatchCursor,
                col(ReportDispatchCursor.tenant_id) == col(SyncSchedule.tenant_id),
            )
            .where(*due)
            .group_by(
                col(SyncSchedule.tenant_id),
                col(ReportDispatchCursor.last_dispatched_at),
            )
            .order_by(
                col(ReportDispatchCursor.last_dispatched_at).asc().nullsfirst(),
                col(SyncSchedule.tenant_id),
            )
            .limit(min(64, available - dispatched))
        ).all()
        # 每轮各租户先给一页；槽多于租户时在下方有界循环公平借用。
        # 只读发现不提前锁head；被锁执行需要跳过候选后继续填槽，最多多探16个。
        skipped_scopes: set[UUID] = set()
        for _round in range(available - dispatched + 16):
            progressed = False
            for tenant_id, _ in tenants:
                if dispatched >= available:
                    break
                session.connection().execute(
                    insert(ReportDispatchCursor)
                    .values(
                        tenant_id=tenant_id,
                        last_dispatched_at=datetime(1970, 1, 1, tzinfo=UTC),
                        fairness_state={},
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id"])
                )
                cursor = session.get(
                    ReportDispatchCursor, tenant_id, with_for_update=True
                )
                assert cursor is not None  # 当前事务已插入租户cursor
                settle_costs(session, cursor)
                heads = []
                for candidate_class in (
                    "realtime",
                    "history",
                    "directory",
                    "maintenance",
                ):
                    if (candidate_class == "realtime" and not live_remaining) or (
                        candidate_class == "history" and not background_remaining
                    ):
                        continue
                    candidate = session.exec(
                        select(SyncSchedule)
                        .where(
                            *due,
                            col(SyncSchedule.tenant_id) == tenant_id,
                            col(SyncSchedule.work_class) == candidate_class,
                            col(SyncSchedule.scope_id).not_in(skipped_scopes),
                        )
                        .order_by(
                            col(SyncSchedule.last_dispatched_at).asc().nullsfirst(),
                            col(SyncSchedule.oldest_pending_at).asc().nullsfirst(),
                        )
                        .limit(1)
                    ).first()
                    if candidate is not None:
                        heads.append(candidate)
                if not heads:
                    continue
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
                current_link = and_(
                    col(ReportSyncJob.start_date) == col(ReportSyncJob.end_date),
                    col(ReportSyncJob.end_date)
                    == func.date(func.timezone(col(SyncScope.timezone), now)),
                )
                if work_class == "directory":
                    if not live_remaining:
                        low_budget.append(~current_link)
                    elif not background_remaining:
                        low_budget.append(current_link)
                if not allow_backfill:
                    # 目录工作类包含历史关联；低预算仍只允许账户当地当天的关联需求。
                    valid_timezone = (
                        select(col(AdvertiserAccount.advertiser_id))
                        .where(
                            col(AdvertiserAccount.tenant_id)
                            == col(SyncScope.tenant_id),
                            col(AdvertiserAccount.advertiser_id)
                            == col(SyncScope.advertiser_id),
                            col(AdvertiserAccount.timezone) == col(SyncScope.timezone),
                        )
                        .correlate(SyncScope)
                        .exists()
                    )
                    low_budget.append(
                        or_(
                            col(ReportSyncJob.kind) != "material_links",
                            and_(
                                col(ReportSyncJob.start_date)
                                == col(ReportSyncJob.end_date),
                                col(ReportSyncJob.start_date)
                                == func.date(
                                    func.timezone(col(SyncScope.timezone), now)
                                ),
                                valid_timezone,
                            ),
                        )
                    )
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
                        col(ReportSyncJob.created_at),
                        col(ReportSyncJob.id),
                    )
                    .limit(1)
                    .with_for_update(of=ReportSyncJob, skip_locked=True)
                ).first()
                if job is None and low_budget:
                    # 暂时没有此物理池的空槽/本轮禁后台，不代表该head没有执行。
                    # 只跳过当前候选，不能把真实due推迟1秒造成每轮释放后空转。
                    skipped_scopes.add(head.scope_id)
                    progressed = True
                    continue
                if job is None:
                    # 单头修正也只在拿到锁并刷新之后做，不能以发现时的旧状态覆盖outcome。
                    locked_head = session.exec(
                        select(SyncSchedule)
                        .where(
                            SyncSchedule.scope_id == head.scope_id,
                            SyncSchedule.work_class == head.work_class,
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
                    # 其他scope可能已派同一个合法BASIC批次，头不应继续占本轮选择。
                    head.next_eligible_at = now + timedelta(seconds=1)
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
                    continue
                execution_heads = _lock_execution_heads(session, job, head, now=now)
                if execution_heads is None:
                    skipped_scopes.add(head.scope_id)
                    progressed = True
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
                from app.modules.reporting.jobs import (
                    MATERIAL_BACKFILL_PAGES_PER_STEP,
                    MATERIAL_PAGES_PER_STEP,
                )

                expected = (
                    (
                        MATERIAL_BACKFILL_PAGES_PER_STEP
                        if work_class == "history"
                        else MATERIAL_PAGES_PER_STEP
                    )
                    if job.kind == "material_day"
                    else 1
                )
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
