"""目录到期头派发；本地发布独立续步，不与平台读取争取发送次数。"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import or_, text, update
from sqlmodel import Session, col, select

from app.core.config import settings
from app.jobs.admission import admission_policy
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.costs import cost_classes, reserve_execution_cost
from app.modules.sync_control.directory_demand import directory_endpoint
from app.modules.sync_control.fairness import (
    WEIGHTS,
    choose_work_class,
    due_tenants,
    lock_tenant_cursor,
)
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncSchedule,
)
from app.modules.sync_control.occupancy import release_dispatch, reserve_dispatch
from app.modules.sync_control.worker_pool import (
    reserve_background_turn,
)


def dispatch_directories_due(
    session: Session,
    redis: Redis,
    *,
    now: datetime,
    limit: int = 8,
    allow_backfill: bool = True,
    max_claims: int | None = None,
) -> int:
    from app.modules.ads.directory_dispatch import (
        directory_step_key,
        enqueue_directory_step,
        is_live_directory,
    )

    # 独立实时读取/本地发布不占后台池；家族锁保护本类窗口，租户cursor保护费用。
    # 只有实际后台候选才在reserve_background_turn中非阻塞领取共享池。
    if (
        not session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:sync-v2-directory',0))"
            )
        )
        .scalar_one()
    ):
        return 0
    # limit是跨扫描共享的总窗口。租约可能同时挂在晋升前后的两个工作类头上，
    # 仍只计一个执行；终态、旧owner/旧代数及已超时Worker不继续占槽。
    # 本地发布也占执行窗口，只是不消耗平台接口额度。
    session.flush()
    inflight = (
        session.connection()
        .execute(
            text("""
        SELECT count(DISTINCT r.id)
        FROM sync_schedule h
        JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
        JOIN ad_directory_run r ON r.id=h.lease_execution_id AND r.tenant_id=h.tenant_id
        WHERE h.work_class IN ('realtime','directory') AND h.lease_token IS NOT NULL
            AND h.lease_execution_generation=r.claim_generation
            AND r.owner_epoch=s.owner_epoch
            AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
            AND (r.claimed_until>:now OR (r.claimed_until IS NULL AND h.leased_until>:now))
        """),
            {"now": now},
        )
        .scalar_one()
    )
    limit = max(0, limit - int(inflight))
    if not limit:
        return 0
    # 窗口约束总在途数；提交批量只约束本事务新增数，不能混为缩小Worker窗口。
    claim_limit = limit
    if max_claims is not None:
        if max_claims < 1:
            raise ValueError("max_claims must be positive")
        claim_limit = min(limit, max_claims)
    due = [
        col(SyncSchedule.active).is_(True),
        col(SyncSchedule.work_class).in_(["realtime", "directory"]),
        col(SyncSchedule.next_eligible_at) <= now,
        or_(
            col(SyncSchedule.leased_until).is_(None),
            col(SyncSchedule.leased_until) <= now,
        ),
        # 报表关联也有directory类，但由报表dispatcher持有；这里只选目录scope。
        # 只探测本头的一条未完成引用，命中scope的PENDING部分索引；
        # 不能把全部历史目录引用物化后再判断当前头是否属于目录。
        select(col(SyncExecutionScope.id))
        .where(
            col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
            col(SyncExecutionScope.directory_run_id).is_not(None),
            col(SyncExecutionScope.status) == "PENDING",
        )
        .correlate(SyncSchedule)
        .limit(1)
        .offset(0)
        .exists(),
    ]
    tenants = due_tenants(session, due, limit=min(64, limit))
    cursors: dict[UUID, ReportDispatchCursor | None] = {}
    count = 0
    background_blocked = False
    # 共享池拒绝后台候选后，额外一轮直接SQL筛选独立live/publisher；
    # 不能逐条跳过历史积压，也不能让最老后台头遮住有空闲执行池的候选。
    for _ in range(claim_limit + 1):
        progressed = False
        blocked_before = background_blocked
        for tenant_id in tenants:
            if count >= claim_limit:
                break
            # 行锁持有到本批提交；复用ORM状态，公平余额仍逐领取更新。
            if tenant_id not in cursors:
                cursors[tenant_id] = lock_tenant_cursor(session, tenant_id)
            cursor = cursors[tenant_id]
            if cursor is None:
                continue
            candidates = []
            for work_class in ("realtime", "directory"):
                row = _candidate(
                    session,
                    tenant_id=tenant_id,
                    work_class=work_class,
                    now=now,
                    independent_only=background_blocked,
                )
                if row is not None:
                    candidates.append(row)
            if not candidates:
                continue
            # 每物理接口维持独立工作类预算；只在该接口同时存在的类间比较。
            endpoint = min(
                candidates,
                key=lambda row: (
                    row[0].last_dispatched_at is not None,
                    row[0].last_dispatched_at or now,
                ),
            )[0].physical_endpoint
            eligible = [
                row for row in candidates if row[0].physical_endpoint == endpoint
            ]
            retained, inflight_classes = cost_classes(
                session, tenant_id=tenant_id, endpoint=endpoint, now=now
            )
            accounting_classes = {
                row[0].work_class for row in eligible
            } | inflight_classes
            chosen_class, state = choose_work_class(
                cursor.fairness_state.get(endpoint, {}),
                [row[0].work_class for row in eligible],
                retained=retained,
                accounting=accounting_classes,
            )
            head, run = next(
                row for row in eligible if row[0].work_class == chosen_class
            )
            # 领取前沿用原完整覆盖/周期校准复验；唤醒不是已投递执行，
            # 真正有页、租约或本代消息的运行仍由原保护拒绝改写。
            from app.modules.ads.directory_backlog import review_unstarted_directory
            from app.modules.sync_control.directory_demand import directory_outcome

            if review_unstarted_directory(session, run, now):
                directory_outcome(session, run)
                session.flush()
                progressed = True
                continue
            terminal = session.exec(
                select(col(AdDirectoryPage.page))
                .where(
                    col(AdDirectoryPage.run_id) == run.id,
                    col(AdDirectoryPage.complete).is_(True),
                )
                .limit(1)
            ).first()
            # 每次正式领取都换代；成功页释放头后，旧页消息不得读取下一页。
            # 先用候选新代预留Redis，准入失败不能推进持久执行身份。
            next_generation = run.claim_generation + 1
            if terminal is None:
                endpoint = directory_endpoint(run)
                reservation_id = directory_step_key(run).replace(
                    f":{run.claim_generation}:page:", f":{next_generation}:page:"
                )
                ready = reserve_dispatch(
                    redis,
                    reservation_id=reservation_id,
                    policy=admission_policy(
                        "reports.integrated",
                        channel="OFFICIAL_API",
                        physical_endpoint=endpoint,
                    ),
                    app_scope=settings.TIKTOK_APP_ID,
                    endpoint=endpoint,
                    rate_group=endpoint,
                    tenant_id=run.tenant_id,
                    advertiser_id=run.advertiser_id,
                )
                if ready.capacity_hint == 0:
                    head.next_eligible_at = now + timedelta(
                        milliseconds=max(1, ready.retry_after_ms)
                    )
                    head.wait_reason = ready.reason
                    continue
                if not is_live_directory(run) and not reserve_background_turn(
                    session,
                    redis,
                    category="directory",
                    now=now,
                    allow_backfill=allow_backfill,
                ):
                    # 共享Worker池与平台发送账本是两个边界；未获本地槽只撤销queued
                    # 占位，不消费代数/改due，也不退还任何已发送的平台额度。
                    background_blocked = True
                    release_dispatch(
                        redis,
                        reservation_id=reservation_id,
                        app_scope=settings.TIKTOK_APP_ID,
                        endpoint=endpoint,
                        rate_group=endpoint,
                        tenant_id=run.tenant_id,
                        advertiser_id=run.advertiser_id,
                    )
                    continue
            run.claim_generation = next_generation
            session.execute(
                update(SyncExecutionScope)
                .where(
                    col(SyncExecutionScope.directory_run_id) == run.id,
                    col(SyncExecutionScope.owner_epoch) == run.owner_epoch,
                    col(SyncExecutionScope.status) == "PENDING",
                )
                .values(execution_generation=run.claim_generation)
            )
            run.claim_token = run.claimed_until = None
            head.leased_until = now + timedelta(seconds=240)
            head.lease_token = uuid4()
            head.lease_execution_id, head.lease_execution_generation = (
                run.id,
                run.claim_generation,
            )
            head.generation += 1
            head.last_dispatched_at, head.wait_reason = now, None
            enqueue_directory_step(
                session,
                run,
                publish_only=terminal is not None,
                dispatch_ready=True,
                dispatched_at=now,
            )
            count += 1
            progressed = True
            cursor.last_dispatched_at = now
            reserve_execution_cost(
                session,
                cursor=cursor,
                execution=run,
                kind="directory",
                endpoint=endpoint,
                work_class=head.work_class,
                eligible_weights={key: WEIGHTS[key] for key in accounting_classes},
                state_after_unit_charge=state,
                reserved_units=1 if terminal is None else 0,
                lease_until=head.leased_until,
            )
        if not progressed and (blocked_before or not background_blocked):
            break
    return count


def _candidate(
    session: Session,
    *,
    tenant_id: UUID,
    work_class: str,
    now: datetime,
    independent_only: bool,
) -> tuple[SyncSchedule, AdDirectoryRun] | None:
    # 发现阶段不加锁：LATERAL内的行锁会在外层排序/LIMIT前锁住整个候选集。
    # 选出ID后再按主键锁定并重新校验所有围栏；冲突回滚小保存点，继续下一候选。
    session.flush()
    excluded: list[UUID] = []
    params = {
        "tenant_id": tenant_id,
        "work_class": work_class,
        "now": now,
        "independent_only": independent_only,
        "excluded": excluded,
    }
    for _ in range(16):
        row = (
            session.connection()
            .execute(
                text("""
        SELECT h.scope_id, h.work_class, picked.id
        FROM sync_schedule h
        JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
        CROSS JOIN LATERAL (
            SELECT r.id, r.created_at
            FROM ad_directory_run r
            WHERE r.tenant_id=h.tenant_id AND r.advertiser_id=s.advertiser_id
                AND r.owner_epoch=s.owner_epoch AND r.id <> ALL(CAST(:excluded AS uuid[]))
                AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
                AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=:now)
                AND (r.claimed_until IS NULL OR r.claimed_until<=:now)
                AND (NOT :independent_only OR (
                    r.workload='live' AND (
                        coalesce(jsonb_array_length(r.query->'ids'),0)>0 OR
                        coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0)
                    ) OR EXISTS (SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE))
                AND EXISTS (
                    SELECT 1 FROM sync_execution_scope e
                    JOIN sync_requirement q ON q.id=e.requirement_id
                    WHERE e.directory_run_id=r.id AND e.scope_id=h.scope_id
                        AND e.tenant_id=h.tenant_id AND e.status='PENDING'
                        AND e.owner_epoch=s.owner_epoch AND q.source=h.work_class
                    LIMIT 1
                )
            ORDER BY r.created_at,r.id
            LIMIT 1
        ) picked
        WHERE h.tenant_id=:tenant_id AND h.work_class=:work_class
            AND h.active IS TRUE AND h.next_eligible_at<=:now
            AND (h.leased_until IS NULL OR h.leased_until<=:now)
        ORDER BY h.last_dispatched_at ASC NULLS FIRST,h.oldest_pending_at,
            picked.created_at,picked.id
        LIMIT 1
    """),
                params,
            )
            .first()
        )
        if row is None:
            return None
        with session.begin_nested() as attempt:
            claimed = (
                session.connection()
                .execute(
                    text("""
SELECT h.scope_id,h.work_class,r.id
FROM sync_schedule h
JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
JOIN sync_execution_scope e ON e.scope_id=h.scope_id AND e.tenant_id=h.tenant_id AND e.status='PENDING' AND e.owner_epoch=s.owner_epoch
JOIN sync_requirement q ON q.id=e.requirement_id AND q.source=h.work_class
JOIN ad_directory_run r ON r.id=e.directory_run_id AND r.tenant_id=h.tenant_id AND r.advertiser_id=s.advertiser_id AND r.owner_epoch=s.owner_epoch
WHERE h.scope_id=:scope_id AND r.id=:run_id AND h.tenant_id=:tenant_id AND h.work_class=:work_class AND h.active IS TRUE
AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=:now)
AND (r.claimed_until IS NULL OR r.claimed_until<=:now)
AND (NOT :independent_only OR (r.workload='live' AND (coalesce(jsonb_array_length(r.query->'ids'),0)>0 OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0)) OR EXISTS(SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE))
ORDER BY h.last_dispatched_at ASC NULLS FIRST,h.oldest_pending_at,r.created_at,r.id LIMIT 1 FOR UPDATE OF h,s,e,q,r SKIP LOCKED
"""),
                    {
                        **params,
                        "scope_id": row.scope_id,
                        "run_id": row.id,
                    },
                )
                .first()
            )
            if claimed is None:
                attempt.rollback()
                excluded.append(row.id)
                continue
            head = session.get(
                SyncSchedule, (row.scope_id, row.work_class), populate_existing=True
            )
            run = session.get(AdDirectoryRun, row.id, populate_existing=True)
            assert head is not None and run is not None
            return head, run
    return None
