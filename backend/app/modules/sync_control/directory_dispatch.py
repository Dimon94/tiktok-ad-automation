"""目录到期头派发；本地发布独立续步，不与平台读取争取发送次数。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import NamedTuple, cast
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


class CandidateKey(NamedTuple):
    scope_id: UUID
    work_class: str
    id: UUID


class DiscoveryCursor(NamedTuple):
    at: datetime
    scope_id: UUID
    until: datetime

    def encode(self) -> str:
        return json.dumps(
            [self.at.isoformat(), str(self.scope_id), self.until.isoformat()]
        )


DISCOVERY_START = datetime.min.replace(tzinfo=UTC)


PAGED_CANDIDATES = """
WITH heads AS MATERIALIZED (
 SELECT h.scope_id,h.work_class,h.tenant_id,s.advertiser_id,s.owner_epoch,
        h.last_dispatched_at,h.oldest_pending_at,
        coalesce(h.last_dispatched_at,h.oldest_pending_at,'epoch'::timestamptz) discovery_at
 FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
 WHERE h.tenant_id=:tenant_id AND h.work_class=:work_class AND h.active IS TRUE
 AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
 AND coalesce(h.last_dispatched_at,h.oldest_pending_at,'epoch'::timestamptz)<=:scan_until
 AND (coalesce(h.last_dispatched_at,h.oldest_pending_at,'epoch'::timestamptz),h.scope_id)
     >(:scan_at,:scan_cursor) AND s.resource LIKE 'directory:%'
 ORDER BY coalesce(h.last_dispatched_at,h.oldest_pending_at,'epoch'::timestamptz),h.scope_id LIMIT 64
)
SELECT h.scope_id,h.work_class,h.last_dispatched_at,h.oldest_pending_at,h.discovery_at,
       picked.id,picked.created_at FROM heads h
LEFT JOIN LATERAL (
 SELECT r.id,r.created_at FROM sync_execution_scope e
 JOIN sync_requirement q ON q.id=e.requirement_id
 CROSS JOIN LATERAL (SELECT * FROM ad_directory_run WHERE id=e.directory_run_id OFFSET 0) r
 WHERE e.scope_id=h.scope_id AND e.status='PENDING' AND e.owner_epoch=h.owner_epoch
 AND e.tenant_id=h.tenant_id AND q.source=h.work_class
 AND r.tenant_id=h.tenant_id AND r.advertiser_id=h.advertiser_id AND r.owner_epoch=h.owner_epoch
 AND r.id <> ALL(CAST(:excluded AS uuid[]))
 AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
 AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=:now)
 AND (r.claimed_until IS NULL OR r.claimed_until<=:now)
 AND (NOT :independent_only OR (r.workload='live' AND (coalesce(jsonb_array_length(r.query->'ids'),0)>0
 OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0))
 OR EXISTS(SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE))
 ORDER BY r.created_at,r.id LIMIT 1
) picked ON TRUE ORDER BY h.discovery_at,h.scope_id
"""


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
            candidates = []
            scan_positions: dict[str, tuple[str, str, str]] = {}
            for work_class in ("realtime", "directory"):
                row = _candidate(
                    session,
                    tenant_id=tenant_id,
                    work_class=work_class,
                    now=now,
                    independent_only=background_blocked,
                    discover_only=True,
                    redis=redis,
                    scan_positions=scan_positions,
                    excluded_run_ids=unavailable_run_ids,
                )
                if row is not None:
                    candidates.append(row)
            if not candidates:
                continue
            # 行锁持有到本批提交；复用ORM状态，公平余额仍逐领取更新。
            if tenant_id not in cursors:
                cursors[tenant_id] = lock_tenant_cursor(session, tenant_id)
            cursor = cursors[tenant_id]
            if cursor is None:
                continue
            # 每物理接口维持独立工作类预算；只在该接口同时存在的类间比较。
            endpoint = min(
                candidates,
                # 新头以真实入队时间参加轮转；NULL不能永久插到所有旧续点前面。
                key=lambda row: (
                    row[0].last_dispatched_at
                    or row[0].oldest_pending_at
                    or row[1].created_at
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
            # 只让公平获选的头消费发现位置，未获选类保留机会。
            # 领取中的SQL回滚仍越过本次尝试；预期后台槽拒绝则在下方归还位置。
            scan_key, scan_before, scan_after = scan_positions[head.work_class]
            redis.set(scan_key, scan_after, ex=86400)
            # 发现阶段不持公平游标锁；取得游标后仅按确定的主键短锁复验。
            claimed = _candidate(
                session,
                tenant_id=tenant_id,
                work_class=head.work_class,
                now=now,
                independent_only=background_blocked,
                selected=(head.scope_id, run.id),
            )
            if claimed is None:
                unavailable_run_ids.add(run.id)
                lock_retries += 1
                progressed = True
                continue
            head, run = claimed
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
                    redis.set(scan_key, scan_before, ex=86400)
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
    discover_only: bool = False,
    selected: tuple[UUID, UUID] | None = None,
    redis: Redis | None = None,
    scan_positions: dict[str, tuple[str, str, str]] | None = None,
    excluded_run_ids: set[UUID] | None = None,
) -> tuple[SyncSchedule, AdDirectoryRun] | None:
    # 发现阶段不加锁：LATERAL内的行锁会在外层排序/LIMIT前锁住整个候选集。
    # 选出ID后再按主键锁定并重新校验所有围栏；冲突回滚小保存点，继续下一候选。
    session.flush()
    excluded: list[UUID] = list(excluded_run_ids or ())
    params = {
        "tenant_id": tenant_id,
        "work_class": work_class,
        "now": now,
        "independent_only": independent_only,
        "excluded": excluded,
    }
    # 先按真实服务时间取64头窄页，再检查执行资格。UUID只消除同时间的歧义，
    # 不能先按随机UUID分区后仅做页内年龄排序，否则整页外的数日旧头会被越过。
    # 两种模式独立保存(服务时间,scope)发现位置；SQL回滚不会丢失无资格页进度。
    # 扫描上界跨调用冻结，避免热点头服务时间不断更新使游标永远追赶now而不回绕。
    # 未获公平工作类/后台槽仍保留完整原位置/上界，真实领取继续复验所有围栏。
    mode = "independent" if independent_only else "all"
    scan_key = f"sync:directory-discovery-age:v1:{tenant_id}:{work_class}:{mode}"
    scan_cursor = DiscoveryCursor(DISCOVERY_START, UUID(int=0), now)
    if redis is not None:
        value = redis.get(scan_key)
        if value:
            stamp, identity, until = json.loads(cast(bytes | str, value))
            scan_cursor = DiscoveryCursor(
                datetime.fromisoformat(stamp),
                UUID(identity),
                datetime.fromisoformat(until),
            )
    wrapped = False
    row: CandidateKey | None
    for _ in range(32):
        if selected:
            row = CandidateKey(
                scope_id=selected[0], work_class=work_class, id=selected[1]
            )
        else:
            rows = (
                session.connection()
                .execute(
                    text(PAGED_CANDIDATES),
                    {
                        **params,
                        "scan_at": scan_cursor.at,
                        "scan_cursor": scan_cursor.scope_id,
                        "scan_until": scan_cursor.until,
                    },
                )
                .all()
            )
            if not rows:
                if wrapped or (
                    scan_cursor.at == DISCOVERY_START and scan_cursor.until >= now
                ):
                    return None
                # 已保存的空轮次也必须在后续调用重开，否则新任务会被旧上界永久挡住。
                scan_cursor, wrapped = (
                    DiscoveryCursor(DISCOVERY_START, UUID(int=0), now),
                    True,
                )
                if redis is not None:
                    redis.set(scan_key, scan_cursor.encode(), ex=86400)
                continue
            # 页间与页内使用同一服务时间顺序；资格过滤仍只探测这64个头。
            found = min(
                (r for r in rows if r.id is not None),
                key=lambda r: (
                    r.discovery_at,
                    r.scope_id,
                ),
                default=None,
            )
            row = (
                CandidateKey(found.scope_id, found.work_class, found.id)
                if found
                else None
            )
            if row is None:
                scan_cursor = DiscoveryCursor(
                    rows[-1].discovery_at, rows[-1].scope_id, scan_cursor.until
                )
                if redis is not None:
                    redis.set(scan_key, scan_cursor.encode(), ex=86400)
                continue
            if scan_positions is not None:
                assert found is not None
                scan_positions[work_class] = (
                    scan_key,
                    scan_cursor.encode(),
                    DiscoveryCursor(
                        found.discovery_at, found.scope_id, scan_cursor.until
                    ).encode(),
                )
        if row is None:
            return None
        if discover_only:
            head = session.get(
                SyncSchedule, (row.scope_id, row.work_class), populate_existing=True
            )
            run = session.get(AdDirectoryRun, row.id, populate_existing=True)
            assert head is not None and run is not None
            return head, run
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
ORDER BY coalesce(h.last_dispatched_at,h.oldest_pending_at,r.created_at),h.oldest_pending_at,r.created_at,r.id LIMIT 1 FOR UPDATE OF h,s,e,q,r SKIP LOCKED
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
                if selected:
                    return None
                excluded.append(row.id)
                continue
            head = session.get(
                SyncSchedule, (row.scope_id, row.work_class), populate_existing=True
            )
            run = session.get(AdDirectoryRun, row.id, populate_existing=True)
            assert head is not None and run is not None
            return head, run
    return None
