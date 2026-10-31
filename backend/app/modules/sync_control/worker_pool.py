"""ads-backfill与ads-directory共用Worker池：统一窗口及跨接口工作类机会。"""

from __future__ import annotations

from datetime import datetime
from time import monotonic
from uuid import UUID
from zoneinfo import ZoneInfo

from redis import Redis
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncScope,
    SyncWorkerPool,
)

POOL_WEIGHTS = {"facts": 2, "links": 1, "directory": 1, "maintenance": 1}


def report_background_category(
    session: Session, job: ReportSyncJob, *, work_class: str, now: datetime
) -> str | None:
    if job.kind != "material_links":
        return "facts" if work_class == "history" else None
    timezones = session.exec(
        select(col(SyncScope.timezone))
        .join(SyncExecutionScope, col(SyncExecutionScope.scope_id) == col(SyncScope.id))
        .where(col(SyncExecutionScope.report_job_id) == job.id)
        .distinct()
    ).all()
    current = (
        bool(timezones)
        and job.start_date == job.end_date
        and all(
            job.end_date == now.astimezone(ZoneInfo(zone)).date() for zone in timezones
        )
    )
    return None if current else "links"


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


def _due_categories(
    session: Session, *, now: datetime, allow_backfill: bool
) -> set[str]:
    # 每个EXISTS只找一个可运行的规范头；未来计划、已占位及本地publisher不抢远端份额。
    rows = (
        session.connection()
        .execute(
            text("""
    SELECT 'facts' WHERE :allow AND EXISTS (
      SELECT 1 FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id
      JOIN sync_execution_scope e ON e.scope_id=s.id JOIN report_sync_job j ON j.id=e.report_job_id
      WHERE h.active AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
        AND h.work_class='history' AND j.kind<>'material_links' AND e.status='PENDING'
        AND e.owner_epoch=s.owner_epoch AND j.owner_epoch=s.owner_epoch
        AND j.status IN ('QUEUED','WAITING','RUNNING') AND j.due_at<=:now
        AND (j.claimed_until IS NULL OR j.claimed_until<=:now))
    UNION ALL SELECT 'links' WHERE :allow AND EXISTS (
      SELECT 1 FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id
      JOIN sync_execution_scope e ON e.scope_id=s.id JOIN report_sync_job j ON j.id=e.report_job_id
      WHERE h.active AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
        AND j.kind='material_links' AND e.status='PENDING' AND e.owner_epoch=s.owner_epoch AND j.owner_epoch=s.owner_epoch
        AND NOT (j.start_date=j.end_date AND j.end_date=(:now AT TIME ZONE s.timezone)::date)
        AND j.status IN ('QUEUED','WAITING','RUNNING') AND j.due_at<=:now
        AND (j.claimed_until IS NULL OR j.claimed_until<=:now))
    UNION ALL SELECT 'directory' WHERE EXISTS (
      SELECT 1 FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id
      JOIN sync_execution_scope e ON e.scope_id=s.id JOIN ad_directory_run r ON r.id=e.directory_run_id
      WHERE h.active AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
        AND e.status='PENDING' AND e.owner_epoch=s.owner_epoch AND r.owner_epoch=s.owner_epoch
        AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
        AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=:now)
        AND (r.claimed_until IS NULL OR r.claimed_until<=:now)
        AND NOT (r.workload='live' AND (coalesce(jsonb_array_length(r.query->'ids'),0)>0
                 OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0))
        AND NOT EXISTS (SELECT 1 FROM ad_directory_page p WHERE p.run_id=r.id AND p.complete IS TRUE))
    UNION ALL SELECT 'maintenance' WHERE EXISTS (
      SELECT 1 FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id
      JOIN sync_execution_scope e ON e.scope_id=s.id JOIN report_balance_run r ON r.id=e.balance_run_id
      WHERE h.active AND h.next_eligible_at<=:now AND (h.leased_until IS NULL OR h.leased_until<=:now)
        AND e.status='PENDING' AND e.owner_epoch=s.owner_epoch AND r.owner_epoch=s.owner_epoch
        AND r.status='ACTIVE' AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=:now)
        AND (r.claimed_until IS NULL OR r.claimed_until<=:now))
    """),
            {"now": now, "allow": allow_backfill},
        )
        .scalars()
    )
    return set(rows)


def _readiness_candidates(
    session: Session, *, category: str, cursor: str, now: datetime, limit: int
) -> list[tuple[UUID, UUID, str, str, bool]]:
    # 先固定窄head页再检查执行资格。先过滤EXISTS再LIMIT会让Merge Join
    # 每次从头重扫执行引用；游标越靠后，持公共池锁的时间反而越长。
    if category in {"facts", "links"}:
        parent, reference = "report_sync_job", "report_job_id"
        family = (
            "h.work_class='history' AND s.resource<>'material_links'"
            if category == "facts"
            else "s.resource='material_links'"
        )
        condition = "p.status IN ('QUEUED','WAITING','RUNNING') AND p.due_at<=:now"
        condition += (
            " AND h.work_class='history' AND p.kind<>'material_links'"
            if category == "facts"
            else " AND p.kind='material_links' AND NOT (p.start_date=p.end_date AND p.end_date=(:now AT TIME ZONE s.timezone)::date)"
        )
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
    pool: SyncWorkerPool,
    category: str,
    now: datetime,
    allow_backfill: bool,
    budget_seconds: float = 0.03,
    probe_limit: int = 64,
) -> set[str]:
    from app.core.config import settings
    from app.jobs.admission import admission_policy
    from app.modules.sync_control.occupancy import dispatch_readiness

    # 当前调用方已在同事务中成功reserve_dispatch，不因自己的queued预留被重复扣而排除。
    eligible = {category}
    cursors = dict(pool.readiness_cursor)
    stop = monotonic() + budget_seconds
    cache = {}
    zero = str(UUID(int=0))
    due = _due_categories(session, now=now, allow_backfill=allow_backfill)
    for candidate in sorted(due - {category}):
        if monotonic() >= stop:
            break
        cursor = cursors.get(candidate, zero)
        rows = _readiness_candidates(
            session, category=candidate, cursor=cursor, now=now, limit=probe_limit
        )
        if not rows and cursor != zero:
            rows = _readiness_candidates(
                session, category=candidate, cursor=zero, now=now, limit=probe_limit
            )
        for index, (scope_id, tenant_id, endpoint, advertiser, runnable) in enumerate(
            rows
        ):
            # 即使SQL恰好用完软预算也至少探测一项，保证游标不会永远停在慢页前。
            if index and monotonic() >= stop:
                break
            cursors[candidate] = str(scope_id)
            if not runnable:
                continue  # 失效/受保护前缀也推进游标，下轮继续探测，不假报可运行。
            key = (tenant_id, endpoint, advertiser)
            if key not in cache:
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
            if cache[key].capacity_hint > 0:
                eligible.add(candidate)
                break
    # 不变更head due，不给未派发类虚扣公平余额；读探测游标独立持久推进。
    pool.readiness_cursor = cursors
    return eligible


def lock_background_pool(session: Session) -> bool:
    """所有派发器先取公共池锁，再取租户cursor，禁止交叉锁序。"""
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
    """同事务内准入后必须立即写对应typed租约；所有调用方共用这一个pool锁。"""
    if category not in POOL_WEIGHTS:
        raise ValueError("unknown background worker category")
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
    if background_lease_count(session, now=now) >= pool.capacity + pool.prefetch_slots:
        return False
    eligible = _ready_categories(
        session,
        redis,
        pool=pool,
        category=category,
        now=now,
        allow_backfill=allow_backfill,
    )
    state = {key: float(pool.fairness_state.get(key, 0)) for key in POOL_WEIGHTS}
    for key in eligible:
        state[key] += POOL_WEIGHTS[key]
    chosen = max(sorted(eligible), key=lambda key: (state[key], POOL_WEIGHTS[key]))
    if chosen != category:
        return False
    state[category] -= sum(POOL_WEIGHTS[key] for key in eligible)
    for key in POOL_WEIGHTS.keys() - eligible:
        state[key] = 0
    pool.fairness_state = state
    return True
