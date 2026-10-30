"""按账户当地当前日监测延迟和覆盖，有界重建缺口；告警与恢复持久可见。"""

import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Any, NotRequired, TypedDict
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.reporting.directory_refresh import (
    PARENT_INTERVAL,
    STATUS_INTERVAL,
    directory_interval,
    refresh_account_directory,
    rule_accounts,
)
from app.modules.reporting.freshness import FRESHNESS, fresh_report_success
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.link_policy import material_link_query
from app.modules.reporting.planner import (
    REFRESH_MARGIN,
    _common,
    _group_by_window,
    _hot_window,
    known_reporting_accounts,
    owned_accounts,
    plan_hot,
    plan_material,
)
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import (
    AdSyncBc,
    ReportSyncHealth,
    ReportSyncHealthEvent,
)
from app.modules.sync_control.dispatch_envelope import enqueue_sync_dispatch
from app.modules.tenants.permissions import require_tenant

logger = logging.getLogger(__name__)
# 一个实时轮次外加执行余量，避免正常等待下一轮被健康修复反复补拉。
SLO = FRESHNESS["ad_day"]
REPAIR_INTERVAL = timedelta(minutes=2)
MAX_REPAIR_ACCOUNTS = 50
REPAIR_BUDGET_SECONDS = 2
HEALTH_BC_TASK = "reporting.check_bc_health"
register_dispatch_task(HEALTH_BC_TASK, "control")
HEALTH_CHUNK_SIZE = 64
HEALTH_MAX_CHUNKS = 16
HEALTH_CYCLE_SECONDS = 120
HEALTH_STAGE_SECONDS = 3
HEALTH_MIN_STAGE_SECONDS = 0.5
HEALTH_FINISH_RESERVE = 0.5
HEALTH_GROWTH_CAP_SECONDS = 600
COVERAGE_STAGES = ("directory", "material")
FRESHNESS_BATCH_SIZE = 512
COVERAGE_REFRESH = timedelta(minutes=3)
COVERAGE_MAX_AGE = timedelta(minutes=5)
COVERAGE_COUNTS = {"missing_directory_ads", "unlinked_material_posts"}
COVERAGE_ISSUES = {"COVERAGE_PENDING", "COVERAGE_STALE", "COVERAGE_QUERY_FAILED"}


class _CoverageStage(TypedDict):
    offset: int
    batch_size: int
    success_streak: int
    # v3已有续点可缺省这两项，沿用原64上限；仅保存运行经验，不复用旧事实。
    growth_cap: NotRequired[int]
    cap_until: NotRequired[str]


class _HealthProgress(TypedDict):
    cycle: str
    scope: str
    at: str
    offset: int
    metrics: dict[str, Any]
    issues: list[str]
    repairs: dict[str, list[str]]
    stages: dict[str, _CoverageStage]
    continuation_seq: int
    retry_not_before: str | None


class _HealthBatchTimeout(Exception):
    """失败批已回滚并保存缩批信息；外层独立事务记录失败并安排有界续投。"""

    def __init__(self, cycle: str):
        self.cycle = cycle
        super().__init__("health observation batch timed out")


def _fresh_parents(session, params, *, kind, keys):
    """在 Python 去重，避免数据库低估当天事实后把父键回连变成平方扫描。"""
    fresh = set()
    # 缺失父 ID 仍由调用方计作缺口，不参与排序或主键探测。
    ordered = sorted(key for key in keys if key[1] is not None)
    for offset in range(0, len(ordered), 256):
        batch = ordered[offset : offset + 256]
        fresh.update(
            session.execute(
                text("""
            SELECT r.advertiser_id,r.parent_id
            FROM unnest(CAST(:advertisers AS text[]),CAST(:parents AS text[]))
              r(advertiser_id,parent_id)
            JOIN LATERAL (
              SELECT true FROM ad_object o
              WHERE o.tenant_id=:t AND o.advertiser_id=r.advertiser_id
                AND o.kind=:kind AND o.remote_id=r.parent_id
                AND o.observed_at >= CASE WHEN r.advertiser_id=ANY(:strict_accounts)
                  THEN :rule_cutoff ELSE :parent_cutoff END LIMIT 1
            ) found ON true
        """),
                {
                    **params,
                    "kind": kind,
                    "advertisers": [key[0] for key in batch],
                    "parents": [key[1] for key in batch],
                },
            ).all()
        )
    return fresh


def _missing_directory_ads(session, params):
    rows = (
        session.execute(
            text("""
        SELECT r.advertiser_id,r.ad_id,r.adgroup_id,r.campaign_id,a.fresh
        FROM ad_report_day r
        LEFT JOIN LATERAL (
          SELECT true AS fresh FROM ad_object o
          WHERE o.tenant_id=r.tenant_id AND o.advertiser_id=r.advertiser_id
            AND o.kind='ad' AND o.remote_id=r.ad_id
            AND o.observed_at >= CASE WHEN r.advertiser_id=ANY(:strict_accounts)
              THEN :rule_cutoff ELSE :status_cutoff END LIMIT 1
        ) a ON true
        WHERE r.tenant_id=:t AND r.advertiser_id=ANY(:ids)
          AND r.stat_date BETWEEN :first_day AND :last_day
          AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
          AND r.spend>0
    """),
            params,
        )
        .mappings()
        .all()
    )
    groups = _fresh_parents(
        session,
        params,
        kind="adgroup",
        keys={
            (row["advertiser_id"], row["adgroup_id"]) for row in rows if row["fresh"]
        },
    )
    campaigns = _fresh_parents(
        session,
        params,
        kind="campaign",
        keys={
            (row["advertiser_id"], row["campaign_id"])
            for row in rows
            if row["fresh"] and (row["advertiser_id"], row["adgroup_id"]) in groups
        },
    )
    return [
        row
        for row in rows
        if not row["fresh"]
        or (row["advertiser_id"], row["adgroup_id"]) not in groups
        or (row["advertiser_id"], row["campaign_id"]) not in campaigns
    ]


def _measurement_params(session, plan, *, now, owned, coverage, directory=True):
    # 常驻连接的通用计划不知道BC账户数组长度，会放弃日期索引；按本轮参数
    # 规划，避免“重启后快、复用后慢”使健康检查超时。仅作用于当前事务。
    session.connection().execute(text("SET LOCAL plan_cache_mode='force_custom_plan'"))
    strict_accounts = (
        rule_accounts(
            session, tenant_id=plan.tenant_id, bc_id=plan.bc_id, accounts=owned
        )
        if coverage and directory
        else set()
    )
    active = set(plan.last_active_advertiser_ids)
    if not coverage:
        # 已在活跃集合的账户无需重复证明事实存在；其余归属账户仍按原事实口径查询。
        unknown = sorted(set(owned) - active)
        for offset in range(0, len(unknown), FRESHNESS_BATCH_SIZE):
            active |= known_reporting_accounts(
                session,
                plan,
                accounts=unknown[offset : offset + FRESHNESS_BATCH_SIZE],
                now=now,
            )
    return {
        "t": plan.tenant_id,
        "bc": plan.bc_id,
        "ids": owned,
        "active": [] if coverage else sorted(active),
        "now": now,
        "cutoff": now - SLO,
        # 全球当地日只可能落在 UTC 前后一天；先走日期索引，再按事实时区精确判断。
        "first_day": now.astimezone(UTC).date() - timedelta(days=1),
        "last_day": now.astimezone(UTC).date() + timedelta(days=1),
        # 热报发布才规划目录，完成读取还需时间；健康期限要留执行余量，
        # 否则正常轮次会被当成缺口触发额外报表补拉，抵消降频。
        "status_cutoff": now - (STATUS_INTERVAL + REFRESH_MARGIN),
        "parent_cutoff": now - (PARENT_INTERVAL + REFRESH_MARGIN),
        "rule_cutoff": now
        - (directory_interval("ad", has_rules=True) + REFRESH_MARGIN),
        "strict_accounts": sorted(strict_accounts),
        "action_cutoff": now - timedelta(minutes=2),
    }


def measure_freshness(session, plan, *, now, accounts=None):
    """分钟检查只读发布水位、筛选与动作队列，不触碰目录或素材关联事实。"""
    owned = owned_accounts(session, plan) if accounts is None else accounts
    params = _measurement_params(session, plan, now=now, owned=owned, coverage=False)
    # 每账户合并两类水位；只在当地零点后的新鲜度窗口读取昨日，平时主键只读两行。
    # 不再让双 JOIN 在大池重复随机读取，或改走整租户历史水位的哈希扫描。
    # 一次求出完整活跃归属集合；只拆SQL执行，不降低覆盖、不分批发布。
    # 每句仍受原5秒限制，调用方10秒总预算覆盖全部批次与后续指标。
    active = sorted(set(owned) & set(params["active"]))
    rows = []
    for offset in range(0, len(active), FRESHNESS_BATCH_SIZE):
        rows.extend(
            session.execute(
                text("""
            WITH active AS (
              SELECT a.advertiser_id,a.timezone,(CAST(:now AS timestamptz) AT TIME ZONE a.timezone)::date AS local_day
              FROM advertiser_account a WHERE a.tenant_id=:t AND a.advertiser_id=ANY(:ids)
                AND a.advertiser_id=ANY(:active))
            SELECT a.advertiser_id,a.timezone,a.local_day,kinds.kind AS report_kind,
              kinds.last_success_at,kinds.previous_success_at
            FROM active a
            LEFT JOIN LATERAL (
              SELECT
                max(s.last_success_at) FILTER (WHERE s.report_kind='ad_day'
                  AND s.stat_date=a.local_day) AS ad_at,
                max(s.last_success_at) FILTER (WHERE s.report_kind='ad_day'
                  AND s.stat_date=a.local_day-1) AS ad_previous_at,
                max(s.last_success_at) FILTER (WHERE s.report_kind='material_day'
                  AND s.stat_date=a.local_day) AS material_at,
                max(s.last_success_at) FILTER (WHERE s.report_kind='material_day'
                  AND s.stat_date=a.local_day-1) AS material_previous_at
              FROM report_sync_state s
              WHERE s.tenant_id=:t AND s.advertiser_id=a.advertiser_id
                AND s.report_kind IN ('ad_day','material_day')
                AND s.stat_date BETWEEN CASE
                  WHEN (CAST(:now AS timestamptz) AT TIME ZONE a.timezone)
                    < a.local_day + CAST(:previous_grace AS interval)
                  THEN a.local_day-1 ELSE a.local_day END AND a.local_day
            ) recent ON true
            CROSS JOIN LATERAL (VALUES
              ('ad_day',recent.ad_at,recent.ad_previous_at),
              ('material_day',recent.material_at,recent.material_previous_at)
            ) kinds(kind,last_success_at,previous_success_at)
        """),
                {
                    **params,
                    "ids": active[offset : offset + FRESHNESS_BATCH_SIZE],
                    # 两种水位共用一次探测；各自是否可借昨日仍由fresh_report_success裁定。
                    "previous_grace": max(FRESHNESS.values()),
                },
            )
            .mappings()
            .all()
        )
    stale = {"ad_day": set(), "material_day": set()}
    times = []
    for row in rows:
        at = fresh_report_success(
            {
                row["local_day"]: row["last_success_at"],
                row["local_day"] - timedelta(days=1): row["previous_success_at"],
            },
            timezone=row["timezone"],
            kind=row["report_kind"],
            now=now,
        )
        # 保留过期今日事实的年龄指标；跨日借用成功时则记录实际采用的时间。
        measured_at = at if at is not None else row["last_success_at"]
        if measured_at is not None:
            times.append(measured_at)
        if at is None:
            stale[row["report_kind"]].add(row["advertiser_id"])
    pending = session.execute(
        text("""
        SELECT count(*) FROM management_task WHERE tenant_id=:t AND bc_id=:bc
          AND status='QUEUED' AND created_at<:action_cutoff
    """),
        params,
    ).scalar_one()
    metrics = {
        "stale_ad_accounts": len(stale["ad_day"]),
        "stale_material_accounts": len(stale["material_day"]),
        "pending_actions": pending,
        "oldest_data_at": min(times).isoformat() if times else None,
    }
    issues = [
        key for key, value in metrics.items() if key != "oldest_data_at" and value
    ]
    repairs = {
        "ad": stale["ad_day"],
        "material": stale["material_day"],
        "links": set(),
        "directory": set(),
    }
    if owned and (
        plan.active_screened_at is None or plan.active_screened_at < now - SLO
    ):
        # 没有“完整筛选确认为零”的证据，空活跃集合是覆盖未知，不能报健康。
        issues.append("ACCOUNT_SCREEN_STALE")
    return metrics, issues, repairs


def measure_coverage(session, plan, *, now, accounts=None, stage=None):
    """有时间标记的完整覆盖快照；分片期间不对外发布部分指标。"""
    owned = owned_accounts(session, plan) if accounts is None else accounts
    if stage is not None and stage not in COVERAGE_STAGES:
        raise ValueError("unknown coverage stage")
    params = _measurement_params(
        session,
        plan,
        now=now,
        owned=owned,
        coverage=True,
        directory=stage != "material",
    )
    # 逐批完整测量；任一批失败仍向外抛出，不能把部分覆盖标记为健康。
    # 同批广告共享的父对象只读取一次；缺失/过期子层不再探测父层。
    # 用集合回连保留逐广告缺口数，避免数据库低估基数引起父链平方扫描。
    missing = []
    directory_accounts = sorted(set(owned))
    for offset in range(0, len(directory_accounts) if stage != "material" else 0, 256):
        missing.extend(
            _missing_directory_ads(
                session,
                {**params, "ids": directory_accounts[offset : offset + 256]},
            )
        )
    # 素材关联同样逐账户主键前缀短探测，避免反连接扫描所有历史映射。
    unlinked = []
    for offset in range(0, len(directory_accounts) if stage != "directory" else 0, 256):
        unlinked.extend(
            session.execute(
                text(f"""
            WITH posts AS MATERIALIZED (
              SELECT DISTINCT r.advertiser_id,r.main_material_id,r.main_material_type FROM material_report_day r
              WHERE r.tenant_id=:t AND r.advertiser_id=ANY(:ids) AND r.spend>0
                AND r.stat_date BETWEEN :first_day AND :last_day
                AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
            )
            SELECT r.advertiser_id,r.main_material_id FROM posts r
            LEFT JOIN LATERAL (
              {material_link_query("r", tenant=":t")} LIMIT 1
            ) linked ON true
            WHERE linked.found IS NULL
        """),
                {**params, "ids": directory_accounts[offset : offset + 256]},
            )
            .mappings()
            .all()
        )
    metrics = {}
    if stage != "material":
        metrics["missing_directory_ads"] = len(missing)
    if stage != "directory":
        metrics["unlinked_material_posts"] = len(unlinked)
    return (
        metrics,
        [key for key, value in metrics.items() if value],
        {
            "ad": set(),
            "material": set(),
            "links": {r["advertiser_id"] for r in unlinked},
            "directory": {r["advertiser_id"] for r in missing},
        },
    )


def measure_health(session, plan, *, now, accounts=None):
    """需要单次完整观测的内部调用复用两种测量；周期任务分别调度。"""
    fresh, issues, repairs = measure_freshness(
        session, plan, now=now, accounts=accounts
    )
    coverage, coverage_issues, coverage_repairs = measure_coverage(
        session, plan, now=now, accounts=accounts
    )
    return (
        fresh | coverage,
        issues + coverage_issues,
        {key: repairs[key] | coverage_repairs[key] for key in repairs},
    )


def _rescue_queued_actions(session, plan, *, now):
    """只重投原本已授权且仍排队的任务正文；业务执行器继续负责幂等、权限和发送栅栏。"""
    params = {
        "t": plan.tenant_id,
        "bc": plan.bc_id,
        "cutoff": now - timedelta(minutes=2),
    }
    # 空业务队列先按租户/BC状态索引返回，避免规划器先全扫数百万历史投递；
    # 存在候选时仍从原消息取最新正文和操作者，保留恢复身份与幂等规则。
    pending = session.execute(
        text("""
        SELECT EXISTS(SELECT 1 FROM management_task
          WHERE tenant_id=:t AND bc_id=:bc AND status='QUEUED' AND created_at<:cutoff)
    """),
        params,
    ).scalar_one()
    if not pending:
        return 0
    rows = session.execute(
        text("""
        SELECT DISTINCT ON (m.id) p.actor_id,p.task_name,p.payload,m.id
        FROM management_task m JOIN pending_dispatch p ON p.tenant_id=m.tenant_id
          AND p.task_name='ad_management.execute' AND p.payload->>'task_id'=m.id::text
        WHERE m.tenant_id=:t AND m.bc_id=:bc AND m.status='QUEUED' AND m.created_at<:cutoff
        ORDER BY m.id,p.available_at DESC LIMIT 50
    """),
        params,
    ).mappings()
    count = 0
    for row in rows:
        enqueue_after_commit(
            session,
            context=TenantContext(plan.tenant_id, row["actor_id"], "operator"),
            task_name=row["task_name"],
            task_key=f"sync-action-repair:{row['id']}:{int(now.timestamp()) // 120}",
            payload=row["payload"],
        )
        count += 1
    return count


def coverage_issues(metrics, *, now, scope=None):
    """完整覆盖是最多五分钟的观测，不用分钟水位伪造其观测时间。"""
    issues = [name for name in sorted(COVERAGE_COUNTS) if metrics.get(name)]
    if scope is not None and metrics.get("coverage_scope") != scope:
        return issues + ["COVERAGE_PENDING"]
    status = metrics.get("coverage_status", "PENDING")
    if status == "FAILED":
        return issues + ["COVERAGE_QUERY_FAILED"]
    if status != "COMPLETE" or not metrics.get("coverage_observed_at"):
        return issues + ["COVERAGE_PENDING"]
    if (
        datetime.fromisoformat(metrics["coverage_observed_at"])
        <= now - COVERAGE_MAX_AGE
    ):
        issues.append("COVERAGE_STALE")
    return issues


def _locked_health(session, plan):
    # 两阶段只在合并时锁行；重新加载最新值，不能拿测量前的 ORM 快照覆盖。
    session.connection().execute(
        text("""
        INSERT INTO report_sync_health(tenant_id,bc_id,status,metrics,issues,observed_at)
        VALUES (:tenant,:bc,'DEGRADED','{}','[]',:unobserved)
        ON CONFLICT(tenant_id,bc_id) DO NOTHING
        """),
        {
            "tenant": plan.tenant_id,
            "bc": plan.bc_id,
            "unobserved": datetime(1970, 1, 1, tzinfo=UTC),
        },
    )
    return session.exec(
        select(ReportSyncHealth)
        .where(
            ReportSyncHealth.tenant_id == plan.tenant_id,
            ReportSyncHealth.bc_id == plan.bc_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def observe_health(
    session, plan, *, now, deadline=None, measurement=None, phase="full", scope=None
):
    remaining = (
        10
        if deadline is None
        else max(0, (deadline - datetime.now(UTC)).total_seconds())
    )
    if measurement is None:
        with query_budget(session, seconds=remaining):
            measurement = measure_health(session, plan, now=now)
    metrics, issues, repairs = measurement
    row = _locked_health(session, plan)
    merged = dict(row.metrics)
    previous_coverage = merged.get("coverage_observed_at")
    if phase in {"full", "coverage"}:
        if previous_coverage and datetime.fromisoformat(previous_coverage) > now:
            return row
        merged.update({key: metrics[key] for key in COVERAGE_COUNTS})
        merged.update(
            coverage_observed_at=now.isoformat(),
            coverage_completed_at=datetime.now(UTC).isoformat(),
            coverage_scope=scope,
            coverage_status="COMPLETE",
        )
    if phase in {"full", "freshness"}:
        if row.observed_at > now:
            return row
        merged.update(
            {key: value for key, value in metrics.items() if key not in COVERAGE_COUNTS}
        )
        merged.setdefault("coverage_observed_at", None)
        merged.setdefault("coverage_completed_at", None)
        merged.setdefault("coverage_scope", None)
        merged.setdefault("coverage_status", "PENDING")
        if phase == "freshness" and merged["coverage_scope"] != scope:
            merged["coverage_status"] = "PENDING"
            merged["coverage_scope"] = scope
        row.observed_at = now
        fresh_issues = [issue for issue in issues if issue not in COVERAGE_COUNTS]
    else:
        fresh_issues = [
            issue
            for issue in row.issues
            if issue not in COVERAGE_COUNTS | COVERAGE_ISSUES
        ]
        if row.observed_at < datetime.now(UTC) - timedelta(minutes=3):
            fresh_issues.append("MONITOR_STALE")
    issues = sorted(
        set(
            fresh_issues
            + coverage_issues(merged, now=max(now, row.observed_at), scope=scope)
        )
    )
    metrics = merged
    status = "DEGRADED" if issues else "HEALTHY"
    changed = row.status != status or row.issues != issues
    if changed:
        session.add(
            ReportSyncHealthEvent(
                tenant_id=plan.tenant_id,
                bc_id=plan.bc_id,
                status=status,
                metrics=metrics,
                issues=issues,
                created_at=now,
            )
        )
        logger.warning(
            "sync health changed tenant=%s bc=%s status=%s issues=%s",
            plan.tenant_id,
            plan.bc_id,
            status,
            issues,
        )
    row.status, row.metrics, row.issues = status, metrics, issues
    repair_at = datetime.now(UTC) if phase == "coverage" else now
    # 完整覆盖每轮最多一次；分钟修复不能用共享 last_repair_at 长期压住关联补齐。
    if issues and (
        phase == "coverage"
        or row.last_repair_at is None
        or row.last_repair_at <= repair_at - REPAIR_INTERVAL
    ):
        # 观测先落在外层事务；补任务遇到规划锁时仅回滚补任务，不丢失健康事实。
        session.flush()
        try:
            previous = (
                session.connection().execute(text("SHOW lock_timeout")).scalar_one()
            )
            session.connection().execute(text("SET LOCAL lock_timeout='100ms'"))
            with query_budget(session, seconds=REPAIR_BUDGET_SECONDS):
                _repair_health_units(
                    session,
                    plan,
                    row,
                    repairs,
                    repair_at,
                    scope=scope,
                    screen_unknown=phase in {"full", "freshness"}
                    and "ACCOUNT_SCREEN_STALE" in fresh_issues,
                )
                session.flush()
            session.connection().execute(
                text("SELECT set_config('lock_timeout', :value, true)"),
                {"value": previous},
            )
        except (DBAPIError, QueryBudgetExceeded) as error:
            if isinstance(error, DBAPIError) and getattr(
                error.orig, "sqlstate", None
            ) not in {"55P03", "57014"}:
                raise
            logger.warning(
                "sync health repair deferred: tenant=%s bc=%s",
                plan.tenant_id,
                plan.bc_id,
            )
    session.flush()
    return row


_DIRECTORY_REPAIR_KEY = "_directory_repair_v1"


def _repair_health_units(session, plan, row, repairs, now, *, scope, screen_unknown):
    """同2秒内轮转两类修复；后续单元失败不能撤销先前已完成的请求。"""
    stop = monotonic() + REPAIR_BUDGET_SECONDS - 0.2
    directory = sorted(repairs.get("directory", ()))
    regular = screen_unknown or any(
        repairs[name] for name in ("ad", "material", "links")
    )
    regular = regular or bool(row.metrics.get("pending_actions"))
    state = dict(row.metrics.get(_DIRECTORY_REPAIR_KEY) or {})
    if directory:
        with session.begin_nested():
            current_scope = scope or _health_scope(
                session, plan, owned_accounts(session, plan), now=now
            )
        if state.get("scope") != current_scope:
            state = {
                "scope": current_scope,
                "after": "",
                "next": "directory",
                "deferred": {},
                "unfinished": [],
            }
    lanes = (["regular"] if regular else []) + (["directory"] if directory else [])
    if state.get("next", "directory") == "directory":
        lanes.reverse()
    for index, lane in enumerate(lanes):
        remaining = stop - monotonic()
        if remaining < 0.1:
            break
        # 下次先给另一类完整机会，不把单户永久压成1秒子限额。
        state["next"] = "regular" if lane == "directory" else "directory"
        if lane == "directory":
            complete = _repair_directory_accounts(
                session, plan, row, directory, now, state, stop
            )
            if index and not complete:
                state["next"] = "directory"
        else:
            try:
                with session.begin_nested(), query_budget(session, seconds=remaining):
                    _repair_health(
                        session, plan, row, repairs, now, screen_unknown=screen_unknown
                    )
                    session.flush()
            except (DBAPIError, QueryBudgetExceeded) as error:
                if isinstance(error, DBAPIError) and getattr(
                    error.orig, "sqlstate", None
                ) not in {"55P03", "57014"}:
                    raise
                if index:
                    state["next"] = "regular"
                logger.info("sync health regular repair deferred: bc=%s", plan.bc_id)
    # 留出的0.2秒用于保存轮转/失败户；尾写取消也只撤销此检查点。
    try:
        with session.begin_nested():
            row.metrics = {**row.metrics, _DIRECTORY_REPAIR_KEY: dict(state)}
            session.flush()
    except (DBAPIError, QueryBudgetExceeded) as error:
        if isinstance(error, DBAPIError) and getattr(
            error.orig, "sqlstate", None
        ) not in {"55P03", "57014"}:
            raise
        logger.info("sync health repair checkpoint deferred: bc=%s", plan.bc_id)


def _repair_directory_accounts(session, plan, row, accounts, now, state, stop):
    deferred = {a: at for a, at in state.get("deferred", {}).items() if a in accounts}
    state["deferred"] = deferred
    unfinished = set(state.get("unfinished", ())) & set(accounts)
    state["unfinished"] = sorted(unfinished)
    retry = sorted(a for a, at in deferred.items() if datetime.fromisoformat(at) <= now)
    available = [a for a in accounts if a not in deferred]
    # after 是最近成功访问户，不代表账户完成。先走后续户再绕回，密集大户
    # 每成功100 ID就合作让出；未完成状态单独保存，不能以移动游标伪报完成。
    ordered = [a for a in available if a > state["after"]] + [
        a for a in available if a <= state["after"]
    ]
    queue = (retry + ordered)[:MAX_REPAIR_ACCOUNTS]
    complete = True
    while queue:
        account = queue.pop(0)
        remaining = stop - monotonic()
        if remaining < 0.1:
            return False
        try:
            with session.begin_nested(), query_budget(session, seconds=remaining):
                # 已入队片由精确pending覆盖排除，续片不重排成功ID。
                result = refresh_account_directory(
                    session,
                    **_common(plan),
                    advertiser_ids=[account],
                    now=now,
                    max_refs=100,
                )
                # 目录有独立游标，不能刷新事实修复的2分钟门槛而压住新过期水位。
                pending = (
                    unfinished - {account}
                    if result.complete
                    else unfinished | {account}
                )
                candidate = {
                    **state,
                    "after": account,
                    "deferred": dict(deferred),
                    "unfinished": sorted(pending),
                }
                candidate["deferred"].pop(account, None)
                # 请求、访问游标与未完集合同一保存点；尾flush失败不能保存假进度。
                row.metrics = {**row.metrics, _DIRECTORY_REPAIR_KEY: candidate}
                session.flush()
        except (DBAPIError, QueryBudgetExceeded) as error:
            sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
            if isinstance(error, DBAPIError) and sqlstate not in {"55P03", "57014"}:
                raise
            # 完整片仍失败的户稍后重试；短尾失败留在原位置，不能误罚慢户。
            if sqlstate == "55P03" or remaining >= REPAIR_BUDGET_SECONDS - 0.4:
                deferred[account] = (now + REPAIR_INTERVAL).isoformat()
            complete = False
            logger.info("sync health directory repair deferred: bc=%s", plan.bc_id)
            continue
        state.update(candidate)
        deferred = state["deferred"]
        unfinished = pending
        if not result.complete:
            # 本轮选中的其他户先获得机会，余量才回补；不同账户仍不超过50个。
            queue.append(account)
    return complete and not unfinished


def _repair_fact_accounts(session, plan, accounts, *, kind, now):
    """在途账户不占救援名额；不同当地日合计仍只补最老的50个缺口。"""
    candidates = []
    for (start, end), ids in _group_by_window(
        session, plan, sorted(accounts), lambda tz: _hot_window(tz, now)
    ).items():
        params = {
            "tenant": plan.tenant_id,
            "bc": plan.bc_id,
            "actor": plan.actor_id,
            "route": json.dumps(plan.frozen_route),
            "kind": kind,
            "start": start,
            "end": end,
        }
        # 与enqueue_jobs的实时覆盖身份一致，先整批读取一次，避免逐账户扫任务。
        # 此处只筛候选，最终入队仍由原覆盖事务锁与唯一键串行复核竞争。
        pending = (
            session.connection()
            .execute(
                text("""
            SELECT advertiser_ids FROM report_sync_job
            WHERE tenant_id=:tenant AND bc_id=:bc AND actor_id=:actor
              AND frozen_route=CAST(:route AS jsonb) AND kind=:kind
              AND priority<=2 AND start_date=:start AND end_date=:end
              AND campaign_ids='[]'::jsonb AND status IN ('QUEUED','RUNNING','WAITING')
            """),
                params,
            )
            .scalars()
        )
        covered = {advertiser for group in pending for advertiser in group}
        remaining = [advertiser for advertiser in ids if advertiser not in covered]
        if not remaining:
            continue
        candidates.extend(
            session.connection()
            .execute(
                text("""
                SELECT a.advertiser_id,s.last_success_at
                FROM unnest(CAST(:ids AS text[])) a(advertiser_id)
                LEFT JOIN LATERAL (
                  SELECT last_success_at FROM report_sync_state s
                  WHERE s.tenant_id=:tenant AND s.advertiser_id=a.advertiser_id
                    AND s.report_kind=:kind AND s.stat_date=:start LIMIT 1
                ) s ON true
                ORDER BY s.last_success_at ASC NULLS FIRST,a.advertiser_id LIMIT :limit
                """),
                params | {"ids": remaining, "limit": MAX_REPAIR_ACCOUNTS},
            )
            .all()
        )
    # 每个日期先取50足以覆盖全局前50；不因多时区放大修复上限。
    candidates.sort(
        key=lambda row: (
            row.last_success_at is not None,
            row.last_success_at or datetime.min.replace(tzinfo=UTC),
            row.advertiser_id,
        )
    )
    return [row.advertiser_id for row in candidates[:MAX_REPAIR_ACCOUNTS]]


def _repair_health(session, plan, row, repairs, now, *, screen_unknown=False):
    # 未知账户选择只决定补修，不是健康指标；必须留在修复保存点与2秒预算内。
    # 覆盖续步不重复筛选，分钟/full阶段才按当前筛选告警补最旧的50个账户。
    if screen_unknown:
        unknown = session.execute(
            text("""
            SELECT a.advertiser_id FROM advertiser_account a
            LEFT JOIN report_sync_state s ON s.tenant_id=a.tenant_id AND s.advertiser_id=a.advertiser_id
              AND s.report_kind='ad_day' AND s.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE a.timezone)::date
            WHERE a.tenant_id=:t AND a.advertiser_id=ANY(:ids)
              AND (s.last_success_at IS NULL OR s.last_success_at<:cutoff)
            ORDER BY s.last_success_at ASC NULLS FIRST, a.advertiser_id LIMIT :limit
        """),
            {
                "t": plan.tenant_id,
                "ids": owned_accounts(session, plan),
                "now": now,
                "cutoff": now - SLO,
                "limit": MAX_REPAIR_ACCOUNTS,
            },
        ).scalars()
        repairs = {**repairs, "ad": repairs["ad"] | set(unknown)}
    count = plan_hot(
        session,
        plan,
        active=_repair_fact_accounts(
            session, plan, repairs["ad"], kind="ad_day", now=now
        ),
        now=now,
    )
    count += plan_material(
        session,
        plan,
        active=_repair_fact_accounts(
            session, plan, repairs["material"], kind="material_day", now=now
        ),
        now=now,
    )
    for (start, end), ids in _group_by_window(
        session,
        plan,
        sorted(repairs["links"])[:MAX_REPAIR_ACCOUNTS],
        lambda tz: _hot_window(tz, now),
    ).items():
        # 已有同冻结范围、同当前日窗口的待领工作保留原退避；证据变化交给
        # wake_link_dependencies复核，健康告警本身不是重新扫描素材的证据。
        covered = set(
            session.connection()
            .execute(
                text("""
            SELECT a.advertiser_id FROM unnest(CAST(:ids AS text[])) a(advertiser_id)
            JOIN LATERAL (
              SELECT j.id FROM report_sync_job j
              WHERE j.tenant_id=:tenant AND j.bc_id=:bc AND j.actor_id=:actor
                AND j.frozen_route=CAST(:route AS jsonb)
                AND j.kind='material_links' AND j.status IN ('QUEUED','WAITING')
                AND j.advertiser_ids=jsonb_build_array(a.advertiser_id)
                AND j.campaign_ids='[]'::jsonb AND j.priority<=2
                AND j.start_date=:start AND j.end_date=:end
                AND (j.claimed_until IS NULL OR j.claimed_until<=:now)
              LIMIT 1 FOR UPDATE SKIP LOCKED
            ) pending ON true
        """),
                {
                    "tenant": plan.tenant_id,
                    "bc": plan.bc_id,
                    "actor": plan.actor_id,
                    "route": json.dumps(plan.frozen_route),
                    "ids": ids,
                    "start": start,
                    "end": end,
                    "now": now,
                },
            )
            .scalars()
        )
        for advertiser in ids:
            if advertiser in covered:
                continue
            count += bool(
                enqueue_job(
                    session,
                    kind="material_links",
                    tier="material",
                    advertiser_ids=[advertiser],
                    start=start,
                    end=end,
                    **_common(plan),
                )
            )
    count += _rescue_queued_actions(session, plan, now=now)
    if count:
        row.last_repair_at = now
    # 调度唤醒与恢复计划同事务，不发外部广告操作、不篡改上次成功时间。
    enqueue_sync_dispatch(
        session,
        context=TenantContext(plan.tenant_id, plan.actor_id, "operator"),
        now=now,
        task_name="reporting.plan_due",
        task_key=f"sync-directory-wake:health:{plan.bc_id}:{now.replace(second=0, microsecond=0).isoformat()}",
        payload={"dispatch_only": True},
    )


def schedule_health(*, database_engine, now=None):
    """Beat 每分钟只登记独立 BC 任务，不把后续 BC 塞进前一个的剩余预算。"""
    if not settings.ADS_SYNC_ENABLED:
        return 0
    now = now or datetime.now(UTC)
    count = 0
    with Session(database_engine) as session:
        keys = session.exec(
            select(AdSyncBc.tenant_id, AdSyncBc.bc_id).where(
                col(AdSyncBc.enabled).is_(True)
            )
        ).all()
    for key in keys:
        try:
            with Session(database_engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='5s'"))
                plan = session.get(AdSyncBc, key)
                if plan is None or not plan.enabled:
                    continue
                enqueue_after_commit(
                    session,
                    context=TenantContext(plan.tenant_id, plan.actor_id, "operator"),
                    task_name=HEALTH_BC_TASK,
                    task_key=f"sync-health-measure:{plan.bc_id}:{int(now.timestamp()) // 60}",
                    payload={"bc_id": plan.bc_id},
                )
                count += 1
        except DBAPIError, DomainError:
            # 单 BC 写入失败不挡后续 BC；Celery 软期限仍向外传播结束本轮。
            logger.exception("sync health dispatch failed tenant=%s bc=%s", *key)
    return count


def _health_lock_key(tenant_id, bc_id):
    return f"reporting:health:bc:v1:{tenant_id}:{bc_id}"


def _health_progress_key(tenant_id, bc_id):
    return f"reporting:health:progress:v3:{tenant_id}:{bc_id}"


def _health_batch_hint_key(tenant_id, bc_id):
    return f"reporting:health:batch-hint:v2:{tenant_id}:{bc_id}"


def _health_scope(session, plan, owned, *, now=None):
    now = now or datetime.now(UTC)
    # 轻量范围元数据能使时区、跨日、规则阈值变化失效；筛选时间不改变覆盖范围。
    zones = (
        session.connection()
        .execute(
            text("""
        SELECT advertiser_id,timezone,(CAST(:now AS timestamptz) AT TIME ZONE timezone)::date
        FROM advertiser_account WHERE tenant_id=:tenant AND advertiser_id=ANY(:ids)
        ORDER BY advertiser_id
    """),
            {"tenant": plan.tenant_id, "ids": sorted(owned), "now": now},
        )
        .all()
    )
    strict = rule_accounts(
        session, tenant_id=plan.tenant_id, bc_id=plan.bc_id, accounts=owned
    )
    return sha256(
        json.dumps(
            [
                str(plan.tenant_id),
                plan.bc_id,
                str(plan.actor_id),
                plan.frozen_route,
                sorted(owned),
                [[account, zone, day.isoformat()] for account, zone, day in zones],
                sorted(strict),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _active_growth_cap(stage, *, now=None):
    until = stage.get("cap_until")
    now = now or datetime.now(UTC)
    if not until or datetime.fromisoformat(until) <= now:
        return HEALTH_CHUNK_SIZE, None
    return max(1, min(HEALTH_CHUNK_SIZE, int(stage["growth_cap"]))), until


def _new_coverage_progress(scope, hints=None):
    hints = hints or {}
    if hints.get("scope") != scope:
        hints = {}
    sizes, caps = hints.get("sizes", {}), hints.get("growth_caps", {})
    now = datetime.now(UTC)
    stages: dict[str, _CoverageStage] = {}
    for name in COVERAGE_STAGES:
        cap, until = _active_growth_cap(caps.get(name, {}), now=now)
        stages[name] = {
            "offset": 0,
            "batch_size": max(1, min(cap, int(sizes.get(name, HEALTH_CHUNK_SIZE)))),
            "success_streak": 0,
        }
        if until is not None:
            stages[name].update(growth_cap=cap, cap_until=until)
    return {
        "cycle": uuid4().hex,
        "scope": scope,
        "at": now.isoformat(),
        "offset": 0,
        "metrics": {},
        "issues": [],
        "repairs": {"ad": [], "material": [], "links": [], "directory": []},
        "stages": stages,
        "continuation_seq": 0,
        "retry_not_before": None,
    }


def _schedule_coverage(session, plan, redis_client, *, row, scope, now):
    key = _health_progress_key(plan.tenant_id, plan.bc_id)
    raw = redis_client.get(key)
    progress = json.loads(raw) if raw else None
    if progress is None or progress["scope"] != scope:
        observed = row.metrics.get("coverage_observed_at")
        if (
            row.metrics.get("coverage_status") == "COMPLETE"
            and row.metrics.get("coverage_scope") == scope
            and observed
            and datetime.fromisoformat(observed) > now - COVERAGE_REFRESH
        ):
            return "COMPLETE"
        hints = redis_client.get(_health_batch_hint_key(plan.tenant_id, plan.bc_id))
        progress = _new_coverage_progress(scope, json.loads(hints) if hints else None)
    if progress.get("retry_not_before") and now < datetime.fromisoformat(
        progress["retry_not_before"]
    ):
        return "WAIT"
    _enqueue_health_continuation(session, plan, redis_client, progress)
    return "CONTINUE"


def _enqueue_health_continuation(
    session, plan, redis_client, progress: _HealthProgress
):
    # 同 offset 的失败重试也必须有新投递键；原 outbox 已发布不能吞掉下一次唤醒。
    progress["continuation_seq"] += 1
    redis_client.set(
        _health_progress_key(plan.tenant_id, plan.bc_id), json.dumps(progress), ex=180
    )
    enqueue_after_commit(
        session,
        context=TenantContext(plan.tenant_id, plan.actor_id, "operator"),
        task_name=HEALTH_BC_TASK,
        task_key=f"sync-health-continue:{plan.bc_id}:{progress['cycle']}:{progress['offset']}:{progress['continuation_seq']}",
        payload={"bc_id": plan.bc_id, "cycle": progress["cycle"]},
    )


def _save_coverage_batch_hint(redis_client, plan, progress):
    # 只保留同范围运行提示；快批写hint不能续长失败经验的绝对期限。
    now = datetime.now(UTC)
    caps = {}
    for name, stage in progress["stages"].items():
        cap, until = _active_growth_cap(stage, now=now)
        if until is not None:
            caps[name] = {"growth_cap": cap, "cap_until": until}
    hint = {
        "scope": progress["scope"],
        "sizes": {
            name: stage["batch_size"] for name, stage in progress["stages"].items()
        },
    }
    if caps:
        hint["growth_caps"] = caps
    redis_client.set(
        _health_batch_hint_key(plan.tenant_id, plan.bc_id),
        json.dumps(hint),
        ex=HEALTH_GROWTH_CAP_SECONDS,
    )


def _log_coverage_stage(
    progress, stage, start, end, size, started, deadline, outcome, *, allocated_seconds
):
    logger.info(
        "health coverage cycle=%s stage=%s offset=%s attempted_end=%s saved_offset=%s "
        "batch_size=%s outcome=%s elapsed=%.3fs cycle_age=%.3fs remaining=%.3fs "
        "budget=%.3fs slice=%s growth_cap=%s cap_until=%s",
        progress["cycle"],
        stage,
        start,
        end,
        progress["stages"][stage]["offset"],
        size,
        outcome,
        monotonic() - started,
        (datetime.now(UTC) - datetime.fromisoformat(progress["at"])).total_seconds(),
        max(0, deadline - monotonic()),
        allocated_seconds,
        "full" if allocated_seconds == HEALTH_STAGE_SECONDS else "tail",
        *_active_growth_cap(progress["stages"][stage]),
    )


def _advance_health(session, plan, redis_client, *, cycle, deadline):
    """分片只保存私有进度；完整范围测完后才发布健康事实和执行修复。"""
    key = _health_progress_key(plan.tenant_id, plan.bc_id)
    raw = redis_client.get(key)
    progress: _HealthProgress | None = json.loads(raw) if raw else None
    if progress is not None and "directory" not in progress["repairs"]:
        # 旧Redis进度只来自coverage；其中ad集合是目录缺口，不能误当事实过期。
        # 原游标/统计/续点保持不变，不把freshness/full测量的ad集合做这种转换。
        progress["repairs"]["directory"] = progress["repairs"]["ad"]
        progress["repairs"]["ad"] = []
    # 旧续投不能新建或推进后来的一轮；Beat 的无 token 任务可接续崩溃进度。
    if cycle is not None and (not progress or progress["cycle"] != cycle):
        return "IGNORED"
    owned = sorted(set(owned_accounts(session, plan)))
    scope = _health_scope(session, plan, owned)
    if progress and datetime.now(UTC) - datetime.fromisoformat(
        progress["at"]
    ) > timedelta(seconds=HEALTH_CYCLE_SECONDS):
        redis_client.delete(key)
        raise QueryBudgetExceeded("health observation cycle expired")
    if progress is None or progress["scope"] != scope:
        redis_client.delete(key)
        return "IGNORED"
    if progress["retry_not_before"] and datetime.now(UTC) < datetime.fromisoformat(
        progress["retry_not_before"]
    ):
        return "WAIT"
    as_of = datetime.fromisoformat(progress["at"])
    total = max(1, len(owned))
    # 原16个账户块各含两种测量，仍允许32个阶段批；不把上限误砍成8个完整块。
    for _ in range(HEALTH_MAX_CHUNKS * len(COVERAGE_STAGES)):
        batch_started = monotonic()
        available_seconds = deadline - batch_started - HEALTH_FINISH_RESERVE
        if progress["offset"] >= total or available_seconds < HEALTH_MIN_STAGE_SECONDS:
            break
        # 按进入时取得的墙钟片分类，监听器递减/50ms取整后的SQL期限不是分类依据。
        # 完整片的3秒包含SAVEPOINT与测量准备；这些耗时不能把完整片变成免缩批尾片。
        allocated_seconds = min(HEALTH_STAGE_SECONDS, available_seconds)
        full_slice = allocated_seconds == HEALTH_STAGE_SECONDS
        stage_name = min(
            COVERAGE_STAGES, key=lambda name: progress["stages"][name]["offset"]
        )
        stage = progress["stages"][stage_name]
        start = stage["offset"]
        before_size = stage["batch_size"]
        end = min(total, start + before_size)
        # SAVEPOINT只包本阶段。另一阶段的紧凑汇总已经保存，不因末阶段取消而重读。
        try:
            with (
                session.begin_nested(),
                query_budget(
                    session,
                    seconds=max(0, allocated_seconds - (monotonic() - batch_started)),
                ),
            ):
                metrics, issues, repairs = measure_coverage(
                    session,
                    plan,
                    now=as_of,
                    accounts=owned[start:end],
                    stage=stage_name,
                )
        except (DBAPIError, QueryBudgetExceeded) as error:
            if (
                isinstance(error, DBAPIError)
                and getattr(error.orig, "sqlstate", None) != "57014"
            ):
                raise
            if not full_slice:
                # 尾片不足原3秒并非测量失败；仅回滚未完成阶段，保留另一阶段与批提示。
                # 正常续投不写FAILED、不缩批，也不把单户送入60秒冷却。
                _log_coverage_stage(
                    progress,
                    stage_name,
                    start,
                    end,
                    before_size,
                    batch_started,
                    deadline,
                    "tail_yield",
                    allocated_seconds=allocated_seconds,
                )
                break
            attempted = end - start
            stage["batch_size"] = max(1, attempted // 2)
            # 同范围密集区失败后，两个邻近快批不能立即恢复刚失败的大批。
            # 只有完整片失败更新cap期限；尾片取消不惩罚，期限到后允许重新学习。
            now = datetime.now(UTC)
            cap, _ = _active_growth_cap(stage, now=now)
            stage["growth_cap"] = min(cap, stage["batch_size"])
            stage["cap_until"] = (
                now + timedelta(seconds=HEALTH_GROWTH_CAP_SECONDS)
            ).isoformat()
            stage["success_streak"] = 0
            # 单账户已无法再拆；不跳账户，也不让重复消息绕过冷却形成忙循环。
            progress["retry_not_before"] = (
                (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
                if attempted == 1
                else None
            )
            redis_client.set(key, json.dumps(progress), ex=180)
            _save_coverage_batch_hint(redis_client, plan, progress)
            _log_coverage_stage(
                progress,
                stage_name,
                start,
                end,
                before_size,
                batch_started,
                deadline,
                "deferred",
                allocated_seconds=allocated_seconds,
            )
            raise _HealthBatchTimeout(progress["cycle"]) from error
        batch_seconds = monotonic() - batch_started
        accumulated = progress["metrics"]
        for name, value in metrics.items():
            if name == "pending_actions":
                # 这是 BC 级计数，采用最后一次完整查询，不能按账户批次相加。
                accumulated[name] = value
            elif name == "oldest_data_at":
                times = [at for at in (accumulated.get(name), value) if at]
                accumulated[name] = min(times) if times else None
            else:
                accumulated[name] = accumulated.get(name, 0) + value
        progress["issues"] = sorted(
            set(progress["issues"])
            | {issue for issue in issues if issue not in metrics}
        )
        for name, accounts in repairs.items():
            progress["repairs"][name] = sorted(
                set(progress["repairs"][name]) | accounts
            )
        stage["offset"] = end
        progress["offset"] = min(part["offset"] for part in progress["stages"].values())
        progress["retry_not_before"] = None
        # 成功不等于仍有扩批余量；贴近3秒期限的批次继续倍增会反复取消并重读。
        # 慢成功已计入完整进度，只缩小下一批；仅连续两批不超过1秒才恢复扩批。
        if batch_seconds > 1.5:
            stage["batch_size"] = max(1, stage["batch_size"] // 2)
        stage["success_streak"] = (
            stage["success_streak"] + 1 if batch_seconds <= 1 else 0
        )
        if stage["success_streak"] >= 2:
            cap, _ = _active_growth_cap(stage)
            stage["batch_size"] = min(cap, stage["batch_size"] * 2)
            stage["success_streak"] = 0
        redis_client.set(key, json.dumps(progress), ex=180)
        if stage["batch_size"] != before_size:
            _save_coverage_batch_hint(redis_client, plan, progress)
        _log_coverage_stage(
            progress,
            stage_name,
            start,
            end,
            before_size,
            batch_started,
            deadline,
            "completed",
            allocated_seconds=allocated_seconds,
        )
    if progress["offset"] < total:
        _enqueue_health_continuation(session, plan, redis_client, progress)
        return "CONTINUE"
    # 观测期间授权或计划范围变化，不能发布旧范围；下次 Beat 从新范围完整重测。
    session.refresh(plan)
    current_owned = sorted(set(owned_accounts(session, plan)))
    if not plan.enabled or _health_scope(session, plan, current_owned) != scope:
        redis_client.delete(key)
        return "IGNORED"
    require_tenant(
        session,
        actor_id=plan.actor_id,
        tenant_id=plan.tenant_id,
        action="reporting_write",
    )
    if datetime.now(UTC) - as_of > timedelta(seconds=HEALTH_CYCLE_SECONDS):
        redis_client.delete(key)
        raise QueryBudgetExceeded("health observation cycle expired")
    metrics = progress["metrics"]
    issues = [
        name for name, value in metrics.items() if name != "oldest_data_at" and value
    ] + progress["issues"]
    repairs = {name: set(accounts) for name, accounts in progress["repairs"].items()}
    observe_health(
        session,
        plan,
        now=as_of,
        measurement=(metrics, issues, repairs),
        phase="coverage",
        scope=scope,
    )
    return "COMPLETE"


def run_bc_health(*, database_engine, redis_client, context, bc_id, cycle=None):
    if not settings.ADS_SYNC_ENABLED:
        return "IGNORED"
    key = (context.tenant_id, bc_id)
    lock = redis_client.lock(_health_lock_key(*key), timeout=30, blocking=False)
    if not lock.acquire(blocking=False):
        return "BUSY"
    try:
        now = datetime.now(UTC)
        deadline = monotonic() + 10
        try:
            with Session(database_engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='5s'"))
                plan = session.get(AdSyncBc, key)
                if plan is None or not plan.enabled:
                    return "IGNORED"
                if plan.actor_id != context.actor_id:
                    raise DomainError(
                        "dispatch_payload_invalid", "健康任务与当前计划身份不符"
                    )
                require_tenant(
                    session,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                    action="reporting_write",
                )
                # 使用执行时刻与当前计划；不携带过期冻结路由，也不共享别的 BC 的时限。
                if cycle is None:
                    with query_budget(session, seconds=max(0, deadline - monotonic())):
                        owned = sorted(set(owned_accounts(session, plan)))
                        scope = _health_scope(session, plan, owned, now=now)
                        measurement = measure_freshness(
                            session, plan, now=now, accounts=owned
                        )
                        session.refresh(plan)
                        if (
                            not plan.enabled
                            or plan.actor_id != context.actor_id
                            or _health_scope(
                                session, plan, owned_accounts(session, plan)
                            )
                            != scope
                        ):
                            return "IGNORED"
                        require_tenant(
                            session,
                            actor_id=context.actor_id,
                            tenant_id=context.tenant_id,
                            action="reporting_write",
                        )
                    row = observe_health(
                        session,
                        plan,
                        now=now,
                        measurement=measurement,
                        phase="freshness",
                        scope=scope,
                    )
                    result = _schedule_coverage(
                        session, plan, redis_client, row=row, scope=scope, now=now
                    )
                else:
                    with query_budget(session, seconds=max(0, deadline - monotonic())):
                        result = _advance_health(
                            session, plan, redis_client, cycle=cycle, deadline=deadline
                        )
            if cycle is not None:
                # COMPLETE 只在事务提交后记录；阶段完成日志不代表完整覆盖已发布。
                logger.info(
                    "health coverage cycle=%s result=%s tenant=%s bc=%s elapsed=%.3fs",
                    cycle,
                    result,
                    *key,
                    monotonic() - (deadline - 10),
                )
            if result == "COMPLETE" and cycle is not None:
                # 数据库提交成功后才删除进度；提交失败/进程崩溃可重试完整发布。
                redis_client.delete(_health_progress_key(*key))
            return result
        except DomainError:
            raise
        except Exception as error:
            logger.exception(
                "sync health check failed tenant=%s bc=%s cycle=%s elapsed=%.3fs",
                *key,
                cycle,
                monotonic() - (deadline - 10),
            )
            # 失败不覆盖上次完整指标；只改变本 BC 的失败可见状态。
            with Session(database_engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='5s'"))
                plan = session.get(AdSyncBc, key)
                if (
                    plan is None
                    or not plan.enabled
                    or plan.actor_id != context.actor_id
                ):
                    return "IGNORED"
                row = _locked_health(session, plan)
                if cycle is not None:
                    row.metrics = {**row.metrics, "coverage_status": "FAILED"}
                    fresh_issues = [
                        issue
                        for issue in row.issues
                        if issue not in COVERAGE_COUNTS | COVERAGE_ISSUES
                    ]
                    row.issues = sorted(
                        set(fresh_issues + coverage_issues(row.metrics, now=now))
                    )
                else:
                    row.issues = sorted(
                        set(
                            ["MONITOR_QUERY_FAILED"]
                            + coverage_issues(row.metrics, now=now)
                        )
                    )
                # 失败不推进任何成功观测时间，分钟失败与覆盖失败不能互相清除。
                row.status = "DEGRADED"
                if cycle is None:
                    # 分钟水位失败不能阻止到期的覆盖观测。失败事实先留在外层，
                    # 授权/范围读取或outbox登记再失败，只回滚此处独立保存点。
                    session.flush()
                    try:
                        with session.begin_nested(), query_budget(session, seconds=2):
                            scope = _health_scope(
                                session, plan, owned_accounts(session, plan)
                            )
                            session.refresh(plan)
                            if (
                                plan.enabled
                                and plan.actor_id == context.actor_id
                                and _health_scope(
                                    session, plan, owned_accounts(session, plan)
                                )
                                == scope
                            ):
                                require_tenant(
                                    session,
                                    actor_id=context.actor_id,
                                    tenant_id=context.tenant_id,
                                    action="reporting_write",
                                )
                                _schedule_coverage(
                                    session,
                                    plan,
                                    redis_client,
                                    row=row,
                                    scope=scope,
                                    now=datetime.now(UTC),
                                )
                                session.flush()
                    except Exception:
                        logger.exception(
                            "sync coverage dispatch deferred after freshness failure"
                        )
                if isinstance(error, _HealthBatchTimeout):
                    raw = redis_client.get(_health_progress_key(*key))
                    progress: _HealthProgress | None = json.loads(raw) if raw else None
                    if (
                        progress is not None
                        and progress["cycle"] == error.cycle
                        and progress["retry_not_before"] is None
                        and datetime.now(UTC) - datetime.fromisoformat(progress["at"])
                        < timedelta(seconds=HEALTH_CYCLE_SECONDS)
                        and _health_scope(session, plan, owned_accounts(session, plan))
                        == progress["scope"]
                    ):
                        require_tenant(
                            session,
                            actor_id=context.actor_id,
                            tenant_id=context.tenant_id,
                            action="reporting_write",
                        )
                        # 一次任务只缩小一次即让出；失败事实与下一次投递在同事务提交。
                        _enqueue_health_continuation(
                            session, plan, redis_client, progress
                        )
            return "FAILED"
    finally:
        lock.release()


@celery_app.task(name="reporting.check_health", time_limit=25, soft_time_limit=20)
def check_health():
    return schedule_health(database_engine=engine)


@celery_app.task(name=HEALTH_BC_TASK, time_limit=25, soft_time_limit=20)
def check_bc_health(*, tenant_id, actor_id, payload):
    validate_dispatch_payload(payload)
    if (
        set(payload) not in ({"bc_id"}, {"bc_id", "cycle"})
        or not isinstance(payload["bc_id"], str)
        or not payload["bc_id"]
        or (
            "cycle" in payload
            and (not isinstance(payload["cycle"], str) or not payload["cycle"])
        )
    ):
        raise DomainError("dispatch_payload_invalid", "健康任务参数无效")
    context = TenantContext(UUID(tenant_id), UUID(actor_id), "operator")
    with Redis.from_url(settings.REDIS_URL) as redis:
        return run_bc_health(
            database_engine=engine,
            redis_client=redis,
            context=context,
            bc_id=payload["bc_id"],
            cycle=payload.get("cycle"),
        )
