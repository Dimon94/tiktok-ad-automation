"""实际后台远端读取共享Worker池；实时读取和本地发布不占此池。"""

from __future__ import annotations

from datetime import datetime
from time import monotonic
from typing import cast
from uuid import UUID

from redis import Redis
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session

from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import (
    SyncWorkerPool,
)

POOL_WEIGHTS = {"facts": 2, "directory": 1, "maintenance": 1}


def report_background_category(
    _session: Session, _job: ReportSyncJob, *, work_class: str, now: datetime
) -> str | None:
    del now
    return "facts" if work_class == "history" else None


def background_lease_count(session: Session, *, now: datetime) -> int:
    """只读typed租约，排队head与worker任一有效都算一个执行；不扫outbox历史。"""
    session.flush()
    return int(
        session.connection()
        .execute(
            text("""
    WITH leased_heads AS MATERIALIZED (
      SELECT DISTINCT h.lease_execution_id id,h.lease_execution_generation generation
      FROM sync_schedule h WHERE h.lease_token IS NOT NULL AND h.leased_until>:now
    ), report_active AS MATERIALIZED (
      SELECT id,owner_epoch FROM report_sync_job
      WHERE execution_pool='background' AND status='RUNNING' AND claimed_until>:now
    ), directory_candidates AS MATERIALIZED (
      -- 提前写明后续相同的活跃状态约束，命中(status,next_attempt_at,claimed_until)索引前缀。
      SELECT r.id FROM ad_directory_run r WHERE r.claimed_until>:now
        AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
      UNION
      SELECT r.id FROM leased_heads h JOIN ad_directory_run r ON r.id=h.id
      WHERE r.claim_generation=h.generation AND r.claimed_until IS NULL
    ), balance_active AS MATERIALIZED (
      SELECT id,owner_epoch FROM report_balance_run WHERE status='ACTIVE' AND claimed_until>:now
    )
    SELECT
      (SELECT count(*) FROM report_active r WHERE NOT EXISTS (
        SELECT 1 FROM sync_execution_scope e JOIN sync_scope s ON s.id=e.scope_id
        WHERE e.report_job_id=r.id AND (e.owner_epoch<>s.owner_epoch OR r.owner_epoch<>s.owner_epoch)))
      + (SELECT count(*) FROM directory_candidates c JOIN ad_directory_run r ON r.id=c.id
         WHERE r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
           AND NOT (r.workload='live' AND (coalesce(jsonb_array_length(r.query->'ids'),0)>0
                    OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0))
           AND NOT EXISTS (SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE)
           AND NOT EXISTS (SELECT 1 FROM sync_execution_scope e JOIN sync_scope s ON s.id=e.scope_id
             WHERE e.directory_run_id=r.id AND (e.owner_epoch<>s.owner_epoch OR r.owner_epoch<>s.owner_epoch)))
      + (SELECT count(*) FROM balance_active r WHERE NOT EXISTS (
        SELECT 1 FROM sync_execution_scope e JOIN sync_scope s ON s.id=e.scope_id
        WHERE e.balance_run_id=r.id AND (e.owner_epoch<>s.owner_epoch OR r.owner_epoch<>s.owner_epoch)))
    """),
            {"now": now},
        )
        .scalar_one()
    )


def _readiness_candidates(
    session: Session, *, category: str, cursor: str, now: datetime, limit: int
) -> list[tuple[UUID, UUID, str, str, bool]]:
    # 先固定窄head页再检查执行资格。先过滤EXISTS再LIMIT会让Merge Join
    # 每次从头重扫执行引用；游标越靠后，持公共池锁的时间反而越长。
    if category == "facts":
        parent, reference = "report_sync_job", "report_job_id"
        family = "h.work_class='history'"
        condition = "p.status IN ('QUEUED','WAITING','RUNNING') AND p.due_at<=:now AND h.work_class='history'"
    elif category == "directory":
        parent, reference = "ad_directory_run", "directory_run_id"
        family = "s.resource LIKE 'directory:%'"
        condition = """p.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
          AND (p.next_attempt_at IS NULL OR p.next_attempt_at<=:now)
          AND NOT (p.workload='live' AND (coalesce(jsonb_array_length(p.query->'ids'),0)>0
                 OR coalesce(jsonb_array_length(p.query->'parent_ids'),0)>0))
          AND NOT EXISTS (SELECT 1 FROM ad_directory_page page WHERE page.run_id=p.id AND page.complete IS TRUE)"""
    else:
        parent, reference = "report_balance_run", "balance_run_id"
        family = "h.work_class='maintenance' AND s.resource='balance'"
        condition = "p.status='ACTIVE' AND (p.next_attempt_at IS NULL OR p.next_attempt_at<=:now)"
    statement = text(f"""
      WITH candidates AS MATERIALIZED (
        SELECT h.scope_id,h.tenant_id,h.physical_endpoint,h.work_class,
          s.advertiser_id,s.owner_epoch,s.timezone
        FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id
        WHERE h.active IS TRUE AND h.next_eligible_at<=:now
          AND (h.leased_until IS NULL OR h.leased_until<=:now)
          AND h.scope_id>:cursor AND s.id>:cursor AND {family}
        ORDER BY h.scope_id LIMIT :limit
      )
      SELECT h.scope_id,h.tenant_id,h.physical_endpoint,h.advertiser_id,
        EXISTS (SELECT 1 FROM sync_execution_scope e JOIN {parent} p ON p.id=e.{reference}
          WHERE e.scope_id=h.scope_id AND e.status='PENDING' AND e.owner_epoch=h.owner_epoch
            AND p.owner_epoch=h.owner_epoch AND (p.claimed_until IS NULL OR p.claimed_until<=:now)
            AND {condition.replace("s.timezone", "h.timezone")}) AS runnable
      FROM candidates h ORDER BY h.scope_id
    """)
    return [
        tuple(row)
        for row in session.connection()
        .execute(
            statement,
            {
                "now": now,
                "cursor": UUID(cursor),
                "limit": limit,
            },
        )
        .all()
    ]


def _ready_categories(
    session: Session,
    redis: Redis,
    *,
    category: str,
    now: datetime,
    allow_backfill: bool,
    budget_seconds: float = 0.03,
    probe_limit: int = 16,
) -> set[str]:
    from app.core.config import settings
    from app.jobs.admission import admission_policy
    from app.modules.sync_control.occupancy import dispatch_readiness

    # 当前调用方已在同事务中成功reserve_dispatch，不因自己的queued预留被重复扣而排除。
    eligible = {category}
    cursor_key = f"sync:background-readiness:v2:{settings.TIKTOK_APP_ID}"
    stop = monotonic() + budget_seconds
    cache = {}
    zero = str(UUID(int=0))
    # 不先做全库EXISTS库存扫描：大库上仅库存就超过30ms，会跳过全部竞争类。
    # 直接探测有界head页，并按当前调用类轮换同行起点，慢页不能固定遮住另一类。
    peers = sorted(POOL_WEIGHTS.keys() - {category})
    if not allow_backfill:
        peers = [peer for peer in peers if peer != "facts"]
    if not peers:
        return eligible
    with redis.pipeline() as sequence:
        sequence.hincrby(cursor_key, f"__turn__:{category}:{int(allow_backfill)}", 1)
        sequence.expire(cursor_key, 86400)
        turn, _ = sequence.execute()
    start = (turn - 1) % len(peers)
    peers = peers[start:] + peers[:start]
    for peer_index, candidate in enumerate(peers):
        # 软预算至少给本次首类一个有界SQL机会；硬SQL/事务预算仍由调用方约束。
        if peer_index and monotonic() >= stop:
            break
        stored = cast(bytes | str | None, redis.hget(cursor_key, candidate))
        cursor = stored.decode() if isinstance(stored, bytes) else (stored or zero)
        rows = _readiness_candidates(
            session, category=candidate, cursor=cursor, now=now, limit=probe_limit
        )
        if not rows and cursor != zero:
            cursor = zero
            rows = _readiness_candidates(
                session, category=candidate, cursor=zero, now=now, limit=probe_limit
            )
        next_cursor = cursor
        probes = 0
        for scope_id, tenant_id, endpoint, advertiser, runnable in rows:
            if not runnable:
                # SQL已经证明的失效前缀只推进位置，不耗一次真正的额度探测机会。
                next_cursor = str(scope_id)
                continue
            key = (tenant_id, endpoint, advertiser)
            if key not in cache:
                if probes and monotonic() >= stop:
                    break  # 未探测的有效候选保留在游标之后，下轮继续。
                probes += 1
                cache[key] = dispatch_readiness(
                    redis,
                    policy=admission_policy(
                        "reports.integrated",
                        channel="OFFICIAL_API",
                        physical_endpoint=endpoint,
                    ),
                    app_scope=settings.TIKTOK_APP_ID,
                    endpoint=endpoint,
                    rate_group=endpoint,
                    tenant_id=tenant_id,
                    advertiser_id=advertiser,
                )
            next_cursor = str(scope_id)
            if cache[key].capacity_hint > 0:
                eligible.add(candidate)
                break
        # 发现位置不承载执行授权；独立于SQL保存点，拒绝或回滚仍越过阻塞前缀。
        # 不能回写池行，否则为保存一个读游标又取得了刚释放的公共行锁。
        redis.hset(cursor_key, candidate, next_cursor)
        redis.expire(cursor_key, 86400)
    # 不变更head due，不给未派发类虚扣公平余额。
    return eligible


def lock_background_pool(session: Session) -> bool:
    """后台准入只try-lock；不能持pool等待租户cursor阻塞独立执行池。"""
    return bool(
        session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:worker-pool:background',0))"
            )
        )
        .scalar_one()
    )


def reserve_background_turn(
    session: Session,
    redis: Redis,
    *,
    category: str,
    now: datetime,
    allow_backfill: bool = True,
) -> bool:
    """成功保留分配锁到租约提交；拒绝释放本次新取的锁，不阻挡其它家族。"""
    if category not in POOL_WEIGHTS:
        raise ValueError("unknown background worker category")
    # PostgreSQL子事务回滚释放本次取得的advisory/行锁。已在外层成功
    # 分配的锁仍保留，不能在对应typed租约尚未提交时让其它事务超发。
    with session.begin_nested() as attempt:
        granted = _reserve_background_turn_locked(
            session,
            redis,
            category=category,
            now=now,
            allow_backfill=allow_backfill,
        )
        if not granted:
            attempt.rollback()
        return granted


def _reserve_background_turn_locked(
    session: Session,
    redis: Redis,
    *,
    category: str,
    now: datetime,
    allow_backfill: bool = True,
) -> bool:
    """同事务内准入后必须立即写对应typed租约；所有调用方共用这一个pool锁。"""
    if not lock_background_pool(session):
        return False
    session.connection().execute(
        insert(SyncWorkerPool)
        .values(pool_name="background", capacity=4, prefetch_slots=0, fairness_state={})
        .on_conflict_do_nothing(index_elements=["pool_name"])
    )
    pool = session.get(
        SyncWorkerPool, "background", with_for_update=True, populate_existing=True
    )
    assert pool is not None
    occupied = background_lease_count(session, now=now)
    if occupied >= pool.capacity + pool.prefetch_slots:
        return False
    eligible = _ready_categories(
        session,
        redis,
        category=category,
        now=now,
        allow_backfill=allow_backfill,
    )
    state = {key: float(pool.fairness_state.get(key, 0)) for key in POOL_WEIGHTS}
    for key in eligible:
        state[key] += POOL_WEIGHTS[key]
    chosen = max(sorted(eligible), key=lambda key: (state[key], POOL_WEIGHTS[key]))
    # 完全空闲时允许借用，避免等待另一家族的派发入口；仍扣借用类份额。
    # 已有工作运行时必须保留公平赢家，确保新到事实能取得下一释放槽位。
    if chosen != category and occupied > 0:
        return False
    state[category] -= sum(POOL_WEIGHTS[key] for key in eligible)
    for key in POOL_WEIGHTS.keys() - eligible:
        state[key] = 0
    pool.fairness_state = state
    return True
