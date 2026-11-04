"""目录到期头派发；本地发布独立续步，不与平台读取争取发送次数。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, NamedTuple
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import or_, text, update
from sqlmodel import Session, col, select

from app.core.config import settings
from app.jobs.admission import admission_policy
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.sync_control.costs import cost_classes, reserve_execution_cost
from app.modules.sync_control.directory_candidates import (
    DiscoveryCursor,
    _candidate,
    completion_candidate,
)
from app.modules.sync_control.directory_demand import directory_endpoint
from app.modules.sync_control.directory_policy import (
    completion_opportunity,
    record_background_grant,
)
from app.modules.sync_control.dispatch_budget import (
    DispatchBudgetConflict,
    EndpointBudgets,
    dispatch_budget_transaction,
)
from app.modules.sync_control.fairness import (
    WEIGHTS,
    choose_work_class,
    due_tenants,
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
    try:
        with dispatch_budget_transaction(session, redis) as budgets:
            return _dispatch_directories_due(
                session,
                redis,
                now=now,
                limit=limit,
                allow_backfill=allow_backfill,
                max_claims=max_claims,
                budgets=budgets,
            )
    except DispatchBudgetConflict:
        return 0


def _dispatch_directories_due(
    session: Session,
    redis: Redis,
    *,
    now: datetime,
    limit: int = 8,
    allow_backfill: bool = True,
    max_claims: int | None = None,
    budgets: EndpointBudgets,
) -> int:
    from app.modules.ads.directory_dispatch import (
        directory_read_pages,
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
    # 远端读取与本地发布各自有界，不能让慢发布的排队租约封死空闲读取Worker。
    # limit仍限制本轮工作量；两个阶段各最多limit个有效执行，不新增Worker或HTTP额度。
    # 租约可能同时挂在晋升前后的两个工作类头上，
    # 仍只计一个执行；终态、旧owner/旧代数及已超时Worker不继续占槽。
    session.flush()
    inflight = {
        bool(publishing): int(count)
        for publishing, count in session.connection()
        .execute(
            text("""
        SELECT publishing,count(*) FROM (
        SELECT DISTINCT r.id,EXISTS(SELECT 1 FROM ad_directory_page p
            WHERE p.run_id=r.id AND p.complete IS TRUE) publishing
        FROM sync_schedule h
        JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
        JOIN ad_directory_run r ON r.id=h.lease_execution_id AND r.tenant_id=h.tenant_id
        WHERE h.work_class IN ('realtime','directory') AND h.lease_token IS NOT NULL
            AND h.lease_execution_generation=r.claim_generation
            AND r.owner_epoch=s.owner_epoch
            AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
            AND (r.claimed_until>:now OR (r.claimed_until IS NULL AND h.leased_until>:now))
        ) executions GROUP BY publishing
        """),
            {"now": now},
        )
        .all()
    }
    read_remaining = max(0, limit - inflight.get(False, 0))
    publish_remaining = max(0, limit - inflight.get(True, 0))
    limit = min(limit, read_remaining + publish_remaining)
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
    count = 0
    unavailable_run_ids: set[UUID] = set()
    background_blocked = False
    lock_retries = 0
    periodic_reviews: dict[UUID, int] = {}
    # 共享池拒绝后台候选后，额外一轮直接SQL筛选独立live/publisher；
    # 不能逐条跳过历史积压，也不能让最老后台头遮住有空闲执行池的候选。
    # 领取失败可继续跳过本轮锁冲突；搜索次数有界，真实派发上限仍是claim_limit。
    for round_index in range(claim_limit + 33):
        # 额外预算只属于真正的行锁冲突，周期延期不能借此整头重扫并长持锁。
        if round_index >= claim_limit + 1 + min(lock_retries, 32):
            break
        progressed = False
        blocked_before = background_blocked
        for tenant_id in tenants:
            # 其它租户的锁冲突不能为本租户的周期维护追加持锁扫描预算。
            if periodic_reviews.get(tenant_id, 0) >= claim_limit + 1:
                continue
            if count >= claim_limit:
                break
            endpoints = _due_directory_endpoints(session, tenant_id, now)
            endpoints.sort(
                key=lambda ep: (
                    budgets.peek_directory_policy(tenant_id, ep).get(
                        "last_granted_at", 0.0
                    ),
                    ep,
                )
            )
            for endpoint in endpoints:
                if not read_remaining and not publish_remaining:
                    break
                phase: Literal["all", "read", "publish"] = (
                    "all"
                    if read_remaining and publish_remaining
                    else "read"
                    if read_remaining
                    else "publish"
                )
                if periodic_reviews.get(tenant_id, 0) >= claim_limit + 1:
                    break  # 多接口仍共享本租户维护上限，不能扩大事务持锁扫描。
                policy_before = budgets.peek_directory_policy(tenant_id, endpoint)
                candidates, scan_positions, completion_id, policy_after = (
                    _discover_endpoint(
                        session,
                        redis,
                        tenant_id=tenant_id,
                        endpoint=endpoint,
                        now=now,
                        independent_only=background_blocked,
                        policy=policy_before,
                        excluded=unavailable_run_ids,
                        phase=phase,
                    )
                )
                if not candidates and policy_after == policy_before:
                    continue
                cursor = budgets.acquire(tenant_id, endpoint)
                if cursor is None:
                    continue
                policy = budgets.directory_policy(tenant_id, endpoint)
                if policy != policy_before:
                    # 发现阶段不持锁；只按锁内最新机会状态重新发现。
                    candidates, scan_positions, completion_id, policy_after = (
                        _discover_endpoint(
                            session,
                            redis,
                            tenant_id=tenant_id,
                            endpoint=endpoint,
                            now=now,
                            independent_only=background_blocked,
                            policy=policy,
                            excluded=unavailable_run_ids,
                            phase=phase,
                        )
                    )
                policy.clear()
                policy.update(policy_after)
                if not candidates:
                    continue
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
                # 只让公平获选的头消费发现位置，未获选类保留机会。
                # 领取中的SQL回滚仍越过本次尝试；预期后台槽拒绝则在下方归还位置。
                scan_position = scan_positions.get(head.work_class)
                _write_scan(redis, scan_position, "advance")
                # 发现阶段不持公平游标锁；取得游标后仅按确定的主键短锁复验。
                claimed = _candidate(
                    session,
                    tenant_id=tenant_id,
                    work_class=head.work_class,
                    now=now,
                    independent_only=background_blocked,
                    selected=(head.scope_id, run.id),
                    endpoint=endpoint,
                    phase=phase,
                )
                if claimed is None:
                    unavailable_run_ids.add(run.id)
                    lock_retries += 1
                    progressed = True
                    continue
                head, run = claimed
                if (
                    head.physical_endpoint != endpoint
                    or directory_endpoint(run) != endpoint
                ):
                    # 发现后还有一次短锁复验；外部校正不能让本次跨接口写成本。
                    unavailable_run_ids.add(run.id)
                    progressed = True
                    continue
                # 领取前沿用原完整覆盖/周期校准复验；唤醒不是已投递执行，
                # 真正有页、租约或本代消息的运行仍由原保护拒绝改写。
                from app.modules.ads.directory_backlog import review_unstarted_directory
                from app.modules.sync_control.directory_demand import directory_outcome

                if review_unstarted_directory(session, run, now):
                    periodic_reviews[tenant_id] = periodic_reviews.get(tenant_id, 0) + 1
                    directory_outcome(session, run)
                    # 复核/延期也已服务这个头；不扣HTTP费用，但必须让回其它头。
                    # 否则同scope的多个周期副本反复占据最老位置，空闲实时池也拿不到任务。
                    head.last_dispatched_at = now
                    _write_scan(redis, scan_position, "restart")
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
                        _write_scan(redis, scan_position, "before")
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
                policy = budgets.directory_policy(tenant_id, endpoint)
                policy["last_granted_at"] = max(
                    now.timestamp(), policy.get("last_granted_at", 0.0) + 0.000001
                )
                if terminal is None and not is_live_directory(run):
                    record_background_grant(
                        policy,
                        advertiser_id=run.advertiser_id,
                        completion=run.id == completion_id,
                    )
                count += 1
                if terminal is None:
                    read_remaining -= 1
                else:
                    publish_remaining -= 1
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
                    reserved_units=directory_read_pages(run) if terminal is None else 0,
                    lease_until=head.leased_until,
                )
                # 服务后从尚未证明无资格的位置重新选最老候选；不能把临时跳过的旧头
                # 留在已服务游标后等整轮结束。真正无资格的窄页前缀仍被缓存。
                _write_scan(redis, scan_position, "restart")
                break  # 每租户每轮最多领取一个，不能由首租户吃完批窗口。
        if not progressed and (blocked_before or not background_blocked):
            break
    return count


ScanPosition = tuple[str, DiscoveryCursor, DiscoveryCursor]
DirectoryCandidate = tuple[SyncSchedule, AdDirectoryRun]


class EndpointDiscovery(NamedTuple):
    candidates: list[DirectoryCandidate]
    positions: dict[str, ScanPosition]
    completion_id: UUID | None
    policy: dict[str, float]


def _write_scan(
    redis: Redis,
    position: ScanPosition | None,
    action: Literal["advance", "before", "restart"],
) -> None:
    # 完成优先没有普通游标位置；成功/退避均不推进普通发现进度。
    if position is None:
        return
    key, before, after = position
    value = before if action == "before" else after
    if action == "restart":
        value = after.restart_qualified()
    redis.set(key, value.encode(), ex=86400)


def _discover_endpoint(
    session: Session,
    redis: Redis,
    *,
    tenant_id: UUID,
    endpoint: str,
    now: datetime,
    independent_only: bool,
    policy: dict[str, float],
    excluded: set[UUID],
    phase: Literal["all", "read", "publish"] = "all",
) -> EndpointDiscovery:
    from copy import deepcopy

    policy_after = deepcopy(policy)
    candidates: list[DirectoryCandidate] = []
    positions: dict[str, ScanPosition] = {}
    completion_id = None
    for work_class in ("realtime", "directory"):
        row = None
        if (
            work_class == "directory"
            and not independent_only
            and phase != "publish"
            and completion_opportunity(policy_after)
        ):
            row = completion_candidate(
                session,
                tenant_id=tenant_id,
                endpoint=endpoint,
                now=now,
                policy=policy_after,
                excluded_run_ids=excluded,
            )
            if row is not None:
                completion_id = row[1].id
        if row is None:
            row = _candidate(
                session,
                tenant_id=tenant_id,
                work_class=work_class,
                now=now,
                independent_only=independent_only,
                discover_only=True,
                redis=redis,
                scan_positions=positions,
                endpoint=endpoint,
                excluded_run_ids=excluded,
                phase=phase,
            )
        if row is not None:
            if row[0].physical_endpoint != directory_endpoint(row[1]):
                excluded.add(row[1].id)
                continue
            candidates.append(row)
    return EndpointDiscovery(candidates, positions, completion_id, policy_after)


def _due_directory_endpoints(
    session: Session, tenant_id: UUID, now: datetime
) -> list[str]:
    # 目录固定六种物理接口；先只读枚举，到具体候选才非阻塞领取该接口预算。
    return list(
        session.connection()
        .execute(
            text("""
        SELECT DISTINCT h.physical_endpoint FROM sync_schedule h
        JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
        WHERE h.tenant_id=:tenant_id AND h.work_class IN ('realtime','directory')
          AND s.resource LIKE 'directory:%' AND h.active IS TRUE
          AND h.next_eligible_at<=:now
          AND (h.leased_until IS NULL OR h.leased_until<=:now)
    """),
            {"tenant_id": tenant_id, "now": now},
        )
        .scalars()
    )
