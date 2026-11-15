"""报表后台入口：冻结授权、分档规划、持久派发与有限租约恢复。"""

import json
import logging
import time
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import Engine, delete, func, text, tuple_, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import verify_route
from app.modules.ads.directory_calibration import calibration_interval
from app.modules.ads.directory_requests import (
    directory_ad_types,
    directory_unblocked_accounts,
    request_directory,
)
from app.modules.ads.material_directory_children import MATERIAL_CHILD_KEY
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting import diagnostics, planner, publish
from app.modules.reporting.jobs import (
    LEASE,
    cancel_retired_report,
    execute_job,
    request_time,
)
from app.modules.reporting.lanes import (
    report_workload,
)
from app.modules.reporting.metrics import parse_decimal
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow
from app.modules.reporting.store_models import (
    AdSyncBc,
    ReportSyncJob,
    ReportSyncPage,
)
from app.modules.sync_control.directory_dispatch import DIRECTORY_DISPATCH_WINDOW
from app.modules.sync_control.dispatch_envelope import enqueue_sync_dispatch

logger = logging.getLogger(__name__)

# 合并旧24+2+4+6个报表候补为统一物理窗口，工作类份额由持久公平游标分配。
REPORT_DISPATCH_WINDOW = 36
# 按小批摊销窗口/租户发现，批间提交释放锁；不能一次锁住整个候补窗口。
DISPATCH_COMMIT_BATCH = 4
DISPATCH_LARGE_COMMIT_BATCH = 8
# 与后轮放大到八条使用相同预算门槛；短时间片不能作为全局降批样本。
DISPATCH_BATCH_SAMPLE_SECONDS = 2.0
# 只记录扫描批量提示，不持有业务租约；空闲后自动恢复正常批量。
DISPATCH_BATCH_HINT_TTL = 300
# 一轮周期目录分批推进；此游标不占用账户筛选或规则相位的进度键。
DIRECTORY_PLAN_PROGRESS = "directory_cycle_v1"
# 批量上限配合查询预算保护数据库；不再按固定分钟额度延迟必要任务发现。
DIRECTORY_PLAN_ACCOUNTS = 64
DIRECTORY_PLAN_SECONDS = 3
DIRECTORY_PLAN_BATCH_INTERVAL = timedelta(seconds=5)

register_dispatch_task("reporting.job_step", "ads-reporting")
register_dispatch_task("reporting.backfill_step", "ads-backfill")
register_dispatch_task("reporting.plan_due", "control")
register_dispatch_task("reporting.plan_continue", "control")


def _context(row):
    return TenantContext(
        tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
    )


def _report_plan_kind(plan) -> str:
    """返回报表任务类型；筛查回放的轻量计划默认使用广告日报入口。"""
    return getattr(plan, "kind", "ad_day")


@contextmanager
def _reader_for(
    *,
    database_engine,
    redis_client,
    plan,
    deadline,
    workload="live",
    reservation_endpoint=None,
):
    from contextlib import nullcontext

    from app.modules.sync_control.occupancy import dispatch_context
    from app.modules.sync_control.schedule import report_endpoint

    reservation = getattr(plan, "claim_token", None)
    # 筛查阶段传入的冻结轻量计划只包含 BC/路由字段，没有报表任务的 kind；
    # 该读取仍走统一 integrated report 物理接口，不能因缺少任务字段阻断热报规划。
    kind = _report_plan_kind(plan)
    endpoint = reservation_endpoint or report_endpoint(
        kind, getattr(plan, "progress", None)
    )
    budget = (
        dispatch_context(
            redis_client,
            reservation_id=reservation,
            scope={
                "app_scope": settings.TIKTOK_APP_ID,
                "endpoint": endpoint,
                "rate_group": endpoint,
                "tenant_id": plan.tenant_id,
                "advertiser_id": plan.advertiser_ids[0],
            },
        )
        if reservation is not None
        else nullcontext()
    )
    with (
        budget,
        open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=_context(plan),
            route=FrozenTikTokRoute.model_validate(plan.frozen_route),
            task_deadline=deadline,
            workload=workload,
        ) as gateway,
    ):
        yield gateway.report_reader


def _active(session, reader, plan, accounts, now, *, days, single_step=False):
    # BC/账户筛选也按各账户本地日期分组，不能把 UTC 日期误作所有账户的今天。
    timezones = planner._timezones(session, plan.tenant_id, accounts)
    windows = {}
    for account, timezone in timezones.items():
        end = planner.local_today(timezone, now)
        windows.setdefault((end - timedelta(days=days - 1), end), []).append(account)
    active = set()
    if plan.bc_report_supported is not False:
        requests = [
            (start, end, tuple(sorted(candidates)))
            for (start, end), candidates in sorted(windows.items())
        ]
        fingerprint = sha256(
            repr((plan.frozen_route, str(plan.actor_id), requests)).encode()
        ).hexdigest()
        key = f"bc:{days}"
        progress: dict[str, Any] = plan.screening_progress.get(key, {})
        if progress.get("fingerprint") != fingerprint:
            progress = {
                "fingerprint": fingerprint,
                "offset": 0,
                "page": 1,
                "active": [],
            }
        active.update(progress["active"])
        for index in range(progress["offset"], len(requests)):
            start, end, candidates = requests[index]
            page = progress["page"] if index == progress["offset"] else 1
            while True:
                result = reader.bc_spend(
                    bc_id=plan.bc_id, start=start, end=end, page=page
                )
                for row in result.rows:
                    account = str((row.get("dimensions") or {}).get("advertiser_id"))
                    spend = parse_decimal((row.get("metrics") or {}).get("spend"))
                    if account in candidates and spend is not None and spend > 0:
                        active.add(account)
                plan.screening_progress = {
                    **plan.screening_progress,
                    key: {
                        "fingerprint": fingerprint,
                        "offset": index if result.next_page else index + 1,
                        "page": result.next_page or 1,
                        "active": sorted(active),
                    },
                }
                if not single_step:
                    session.flush()
                if single_step and (
                    result.next_page is not None or index + 1 < len(requests)
                ):
                    from app.modules.reporting.planning_steps import ScreenYield

                    raise ScreenYield()
                if result.next_page is None:
                    break
                page = result.next_page
        plan.screening_progress = {
            k: v for k, v in plan.screening_progress.items() if k != key
        }
        if days == 1:
            plan.active_screened_at = now
        return sorted(active)
    requests = []
    for (start, end), candidates in sorted(windows.items()):
        candidates = sorted(candidates)
        for offset in range(0, len(candidates), 5):
            for s, e in planner._shards(start, end):
                requests.append((tuple(candidates[offset : offset + 5]), s, e))
    # 日期或冻结范围变化后重新筛选；同一轮失败只重读尚未完成的请求。
    fingerprint = sha256(
        repr((plan.frozen_route, str(plan.actor_id), requests)).encode()
    ).hexdigest()
    key = str(days)
    progress: dict[str, Any] = plan.screening_progress.get(key, {})
    if progress.get("fingerprint") != fingerprint:
        progress = {"fingerprint": fingerprint, "offset": 0, "active": []}
    active.update(progress["active"])
    for index in range(progress["offset"], len(requests)):
        batch, start, end = requests[index]
        page = progress.get("page", 1) if index == progress["offset"] else 1
        while True:
            result = reader.account_day(
                advertiser_ids=batch, start=start, end=end, page=page
            )
            for row in result.rows:
                account = str(row.get("dimensions", {}).get("advertiser_id", ""))
                spend = parse_decimal(row.get("metrics", {}).get("spend"))
                if account in batch and spend is not None and spend > 0:
                    active.add(account)
            plan.screening_progress = {
                **plan.screening_progress,
                key: {
                    "fingerprint": fingerprint,
                    "offset": index if result.next_page else index + 1,
                    "page": result.next_page or 1,
                    "active": sorted(active),
                },
            }
            if not single_step:
                session.flush()
            if single_step and (
                result.next_page is not None or index + 1 < len(requests)
            ):
                from app.modules.reporting.planning_steps import ScreenYield

                raise ScreenYield()
            if result.next_page is None:
                break
            page = result.next_page
    plan.screening_progress = {
        k: v for k, v in plan.screening_progress.items() if k != key
    }
    if days == 1:
        plan.active_screened_at = now
    return sorted(active)


def _advance(due, now, interval):
    return due + (max(0, (now - due) // interval) + 1) * interval


def _dispatch_directories(
    session, now, *, allow_backfill=True, max_claims=None, redis_client=None
):
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due

    with (
        nullcontext(redis_client)
        if redis_client is not None
        else Redis.from_url(settings.REDIS_URL)
    ) as redis:
        return dispatch_directories_due(
            session,
            redis,
            now=now,
            limit=DIRECTORY_DISPATCH_WINDOW,
            allow_backfill=allow_backfill,
            max_claims=max_claims,
        )


PLANNING_ENTRY_LOCK = "reporting:planner:entry:v1"
PLANNING_CONTINUATION = "reporting:planner:continuation:v1"


def _release_planning_key(redis_client, key, owner):
    # 租约过期后不得删除另一进程后来取得的锁；网络故障由TTL恢复，不遮盖原错。
    try:
        redis_client.eval(
            "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
            1,
            key,
            owner,
        )
    except RedisError:
        logger.warning("report planning signal: outcome=release_unavailable")


def _continuation_contract(payload):
    validate_dispatch_payload(payload)
    try:
        if set(payload) != {"continuation_id", "expires_at"}:
            raise ValueError()
        identity = str(UUID(payload["continuation_id"]))
        expires = datetime.fromisoformat(payload["expires_at"])
        if expires.tzinfo is None:
            raise ValueError()
    except ValueError, TypeError, KeyError:
        raise DomainError("dispatch_payload_invalid", "规划续点参数无效") from None
    return identity, expires


def _planning_budget_error(error: BaseException) -> bool:
    return isinstance(error, QueryBudgetExceeded) or (
        isinstance(error, DBAPIError)
        and getattr(error.orig, "sqlstate", None) in {"57014", "55P03", "40P01"}
    )


def plan_due_runs(*, database_engine, redis_client, now=None, continuation=None):
    if not settings.ADS_SYNC_ENABLED:
        return 0
    started_at = datetime.now(UTC)
    stop = time.monotonic() + 10
    owner = str(uuid4())
    if not redis_client.set(PLANNING_ENTRY_LOCK, owner, nx=True, ex=35):
        logger.info(
            "report planning signal: outcome=entry_busy mode=%s",
            "realtime_continue" if continuation else "regular",
        )
        return 0
    try:
        if continuation is not None:
            identity, expires = _continuation_contract(continuation)
            marker = redis_client.get(PLANNING_CONTINUATION)
            if isinstance(marker, bytes):
                marker = marker.decode()
            if marker != identity or expires.tzinfo is None:
                logger.info(
                    "report planning signal: outcome=obsolete mode=realtime_continue"
                )
                return 0
            left = (expires - datetime.now(UTC)).total_seconds()
            if left < 12:
                # 迟到续点不能持锁跨过下一普通轮；不能用排队延迟吞掉维护机会。
                logger.info(
                    "report planning signal: outcome=expired_or_late mode=realtime_continue"
                )
                return 0
            stop = min(stop, time.monotonic() + left - 2)
        result = _run_planning_entry(
            database_engine=database_engine,
            redis_client=redis_client,
            now=now,
            realtime_only=continuation is not None,
            stop=stop,
            started_at=started_at,
        )
        return result
    finally:
        if continuation is not None and isinstance(continuation, dict):
            _release_planning_key(
                redis_client,
                PLANNING_CONTINUATION,
                str(continuation.get("continuation_id", "")),
            )
        _release_planning_key(redis_client, PLANNING_ENTRY_LOCK, owner)


def _schedule_planning_continuation(
    database_engine, redis_client, now, started_at, *, stop=None
):
    # 普通轮最多补一个中间续点；过期消息不追赶，持久阶段由下个Beat正常接管。
    stop = time.monotonic() + 1 if stop is None else stop
    if stop - time.monotonic() < 0.2:
        logger.info("report planning signal: outcome=schedule_no_budget mode=regular")
        return
    identity = str(uuid4())
    expires = started_at + timedelta(seconds=30)
    if datetime.now(UTC) >= expires:
        return
    created = False
    try:
        with Session(database_engine) as session, session.begin():
            with query_budget(session, seconds=max(0, stop - time.monotonic())):
                plans = planner.due_plans(session, now=now, limit=1, realtime_only=True)
                if not plans:
                    return
                if not redis_client.set(
                    PLANNING_CONTINUATION, identity, nx=True, ex=60
                ):
                    logger.info(
                        "report planning signal: outcome=coalesced mode=regular"
                    )
                    return
                created = True
                dispatch_id = enqueue_sync_dispatch(
                    session,
                    context=_context(plans[0]),
                    now=datetime.now(UTC),
                    task_name="reporting.plan_continue",
                    task_key=f"report-planning-continue:{identity}",
                    payload={
                        "continuation_id": identity,
                        "expires_at": expires.isoformat(),
                    },
                )
                dispatch = session.get(PendingDispatch, dispatch_id)
                assert dispatch is not None
                dispatch.available_at = started_at + timedelta(seconds=15)
                session.flush()
        logger.info("report planning signal: outcome=scheduled mode=regular")
    except BaseException as error:
        if created:
            _release_planning_key(redis_client, PLANNING_CONTINUATION, identity)
        if isinstance(error, DomainError) and error.code == "dispatch_expired":
            return
        if not _planning_budget_error(error):
            raise
        logger.info(
            "report planning signal: outcome=schedule_budget_exhausted mode=regular"
        )


def _run_planning_entry(
    *,
    database_engine,
    redis_client,
    now=None,
    realtime_only=False,
    stop=None,
    started_at=None,
):
    started_at = started_at or datetime.now(UTC)
    now = now or datetime.now(UTC)
    allow_backfill = not realtime_only and planner.backfill_allowed(redis_client, now)
    started = time.monotonic()
    stop = started + 10 if stop is None else stop
    deadline = datetime.now(UTC) + timedelta(seconds=max(0, stop - started))
    # 额度紧张优先让出历史补数，不静默把止损数据降成小时级。
    hot, material = planner.HOT_INTERVAL, planner.MATERIAL_INTERVAL
    if not realtime_only:
        from app.modules.reporting.partitions import maintain_partitions

        maintain_partitions(database_engine, today=now.date(), limit=2)
    try:
        with (
            Session(database_engine) as session,
            session.begin(),
            query_budget(session, seconds=max(0, stop - time.monotonic())),
        ):
            if not realtime_only:
                planner.ensure_bc_plans(session, now=now)
            keys = [
                (p.tenant_id, p.bc_id)
                for p in planner.due_plans(
                    session, now=now, realtime_only=realtime_only
                )
            ]
            session.flush()
    except (DBAPIError, QueryBudgetExceeded) as error:
        if not _planning_budget_error(error):
            raise
        logger.info("report planning signal: outcome=prepare_budget_exhausted")
        return 0
    prepare_seconds = time.monotonic() - started
    if not realtime_only:
        # 在慢派发/阶段之前确认仍有实时工作并持久化一次续点；不能赌尾部有
        # 剩余预算，否则最需要续跑的满预算普通轮反而永远无法产生信号。
        _schedule_planning_continuation(
            database_engine, redis_client, now, started_at, stop=stop
        )
    dispatch_started = time.monotonic()
    dispatched = _dispatch_planning_entry(
        database_engine,
        now,
        # 首次补位只占短片，慢目录不能每轮吃掉全部维护/规划机会；尾部仍可补位。
        min(stop, time.monotonic() + 1),
        allow_backfill=allow_backfill,
        include_directories=not realtime_only,
    )
    dispatch_seconds = time.monotonic() - dispatch_started
    maintenance_started = time.monotonic()
    # 每轮每BC只推进一个阶段，预算有余继续下一轮；不以账户数量人为限速。
    remaining = list(keys)
    metrics = {"completed": 0, "deferred": 0}
    maintenance_seconds = time.monotonic() - maintenance_started
    steps_started = time.monotonic()
    step_budget = max(0, stop - steps_started)
    screen_budget = min(8, max(2, stop - time.monotonic() - 0.1))
    while remaining and stop - time.monotonic() >= 2:
        next_round = []
        for key in remaining:
            if stop - time.monotonic() < 2:
                break
            try:
                worked = _plan_one(
                    database_engine,
                    redis_client,
                    key,
                    now,
                    deadline,
                    hot,
                    material,
                    allow_backfill=allow_backfill,
                    one_step=True,
                    time_budget=max(0, stop - time.monotonic()),
                    metrics=metrics,
                    realtime_only=realtime_only,
                    prefer_facts=metrics.get("screen_attempts", 0) > 0
                    # 每BC两类事实各有轮转机会；全局仅两次会被两个BC的hot
                    # 耗完，使下一慢筛查再次吞掉所有material的入口余量。
                    and metrics.get("facts_since_screen", 0) < 2 * len(keys),
                )
            except SoftTimeLimitExceeded:
                return dispatched
            except (DBAPIError, QueryBudgetExceeded) as error:
                if not _planning_budget_error(error):
                    logger.exception("report plan failed: tenant=%s bc=%s", *key)
                    _defer_failed_plan(database_engine, key, now, stop=stop)
                    continue
                # 元数据/外层总片耗尽时没有当前业务批可缩；保留持久游标交下轮。
                logger.info(
                    "report planning entry stage deferred: bc=%s budget_remaining=%.3f",
                    key[1],
                    max(0, stop - time.monotonic()),
                )
                continue
            except Exception:
                logger.exception("report plan failed: tenant=%s bc=%s", *key)
                _defer_failed_plan(database_engine, key, now, stop=stop)
                continue
            if worked:
                next_round.append(key)
        remaining = next_round
    steps_seconds = time.monotonic() - steps_started
    dispatch_started = time.monotonic()
    dispatched += _dispatch_planning_entry(
        database_engine,
        now,
        stop,
        allow_backfill=allow_backfill,
        include_directories=not realtime_only,
    )
    dispatch_seconds += time.monotonic() - dispatch_started
    logger.info(
        "report planning entry finished: plans=%d completed=%d deferred=%d prepare_ms=%.1f maintenance_ms=%.1f dispatch_ms=%.1f steps_ms=%.1f metadata_ms=%.1f work_ms=%.1f stage_ms=%.1f total_ms=%.1f step_budget=%.3f screen_budget=%.3f mode=%s",
        len(keys),
        metrics["completed"],
        metrics["deferred"],
        prepare_seconds * 1000,
        maintenance_seconds * 1000,
        dispatch_seconds * 1000,
        steps_seconds * 1000,
        metrics.get("metadata_s", 0) * 1000,
        metrics.get("work_s", 0) * 1000,
        metrics.get("stage_s", 0) * 1000,
        (time.monotonic() - started) * 1000,
        step_budget,
        screen_budget,
        "realtime_continue" if realtime_only else "regular",
    )
    return dispatched


def _dispatch_planning_entry(
    database_engine, now, stop, *, allow_backfill, include_directories
):
    remaining = stop - time.monotonic()
    if remaining < 0.2:
        return 0
    try:
        return _dispatch_pending(
            database_engine,
            now,
            allow_backfill=allow_backfill,
            include_directories=include_directories,
            budget_seconds=remaining,
        )
    except (DBAPIError, QueryBudgetExceeded) as error:
        if not _planning_budget_error(error):
            raise
        logger.info("report planning signal: outcome=dispatch_budget_exhausted")
        return 0


def _defer_failed_plan(
    database_engine: Any, key: tuple[UUID, str], now: datetime, *, stop=None
) -> None:
    stop = time.monotonic() + 1 if stop is None else stop
    if stop - time.monotonic() < 0.2:
        return
    try:
        with (
            Session(database_engine) as session,
            session.begin(),
            query_budget(session, seconds=max(0, stop - time.monotonic())),
        ):
            plan = session.exec(
                select(AdSyncBc)
                .where(col(AdSyncBc.tenant_id) == key[0], col(AdSyncBc.bc_id) == key[1])
                .with_for_update(skip_locked=True)
            ).one_or_none()
            if plan is None:
                return
            plan.disabled_reason = "plan_failed"
            retry_at = now + timedelta(minutes=5)
            progress = plan.screening_progress.get("planning_steps_v1")
            if progress:
                progress = {
                    k: v for k, v in progress.items() if k != "screen_waiting_since"
                }
                plan.screening_progress = {
                    **plan.screening_progress,
                    "planning_steps_v1": {
                        **progress,
                        "retry_at": retry_at.isoformat(),
                        "last_turn": datetime.now(UTC).isoformat(),
                    },
                }
            _defer_plan(plan, retry_at)
            plan.next_material_at = max(plan.next_material_at, retry_at)

            session.flush()
    except (DBAPIError, QueryBudgetExceeded) as error:
        if not _planning_budget_error(error):
            raise
        logger.info("report planning signal: outcome=failure_state_budget_exhausted")


def _defer_plan(plan, retry_at):
    # 账户筛选、历史和目录一起退避；素材使用已知活跃账户独立投递，
    # 不能因上游筛选退避而整轮漏投。
    for field in (
        "next_hot_at",
        "next_daily_at",
        "next_directory_at",
    ):
        setattr(plan, field, max(getattr(plan, field), retry_at))


def _plan_directory_step(session, plan, *, accounts, now, budget=None):
    """逐账户保存点合作让出；成功任务与前缀游标同事务提交，慢尾户不撤销前项。"""
    started = time.monotonic()
    budget = min(
        DIRECTORY_PLAN_SECONDS, budget if budget is not None else DIRECTORY_PLAN_SECONDS
    )
    # 内外不能同时打满3秒：外层还需保存目录游标/阶段状态并提交。
    work_budget = max(0, budget - 0.3)
    work_stop = started + work_budget
    # 撤销固定分钟门槛，只清理其私有标记，保留目录游标及其他业务进度。
    if "directory_batch_not_before_v1" in plan.screening_progress:
        plan.screening_progress = {
            key: value
            for key, value in plan.screening_progress.items()
            if key != "directory_batch_not_before_v1"
        }
    accounts = sorted(set(accounts))
    scope = sha256(
        json.dumps(
            [
                str(plan.tenant_id),
                plan.bc_id,
                str(plan.actor_id),
                plan.frozen_route,
                accounts,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    state: dict[str, Any] = dict(
        plan.screening_progress.get(DIRECTORY_PLAN_PROGRESS) or {}
    )
    if state.get("scope") != scope:
        state = {
            "scope": scope,
            "anchor": plan.next_directory_at.isoformat(),
            "after": "",
            "batch_size": DIRECTORY_PLAN_ACCOUNTS,
            "deferred": {},
        }
    anchor = datetime.fromisoformat(state["anchor"])
    owned = set(accounts)
    deferred = {a: at for a, at in state.get("deferred", {}).items() if a in owned}
    retry = sorted(a for a, at in deferred.items() if datetime.fromisoformat(at) <= now)
    fresh = [a for a in accounts if a > state["after"] and a not in deferred]
    # 64只是单轮上限，不因上一轮尾部耗尽永久缩小；实际能做多少由剩余时间决定。
    state["batch_size"] = DIRECTORY_PLAN_ACCOUNTS
    batch = (retry + fresh)[:DIRECTORY_PLAN_ACCOUNTS]
    completed = 0
    for account in batch:
        remaining_budget = work_stop - time.monotonic()
        if remaining_budget < 0.1:
            break  # 未开始账户继续留在原游标之后，不误记为失败或完成。
        try:
            with (
                session.begin_nested(),
                query_budget(session, seconds=remaining_budget),
            ):
                due = directory_unblocked_accounts(
                    session,
                    tenant_id=plan.tenant_id,
                    bc_id=plan.bc_id,
                    actor_id=plan.actor_id,
                    frozen_route=plan.frozen_route,
                    advertiser_ids=[account],
                    now=now,
                )
                request_directory(
                    session,
                    tenant_id=plan.tenant_id,
                    bc_id=plan.bc_id,
                    actor_id=plan.actor_id,
                    frozen_route=plan.frozen_route,
                    advertiser_ids=due,
                    scope="directory",
                    now=now,
                    periodic_intervals={
                        item: calibration_interval(plan, item, now) for item in due
                    },
                    ad_types=directory_ad_types(
                        session,
                        tenant_id=plan.tenant_id,
                        advertiser_ids=due,
                        active_ids=plan.last_active_advertiser_ids,
                    ),
                )
                session.flush()
        except (DBAPIError, QueryBudgetExceeded) as error:
            sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
            if isinstance(error, DBAPIError) and sqlstate not in {
                "57014",
                "55P03",
                "40P01",
            }:
                raise
            lock_conflict = sqlstate in {"55P03", "40P01"}
            # 用配置完整片判定；外层只剩2秒时首户也只是短片，不能误罚五分钟。
            full_account_slice = remaining_budget >= DIRECTORY_PLAN_SECONDS - 0.3 - 0.1
            if lock_conflict or full_account_slice:
                # 锁争用短退避；真正占满单户完整片的账户保留原五分钟延期。
                deferred[account] = (
                    now + timedelta(seconds=5)
                    if lock_conflict
                    else now + timedelta(minutes=5)
                ).isoformat()
            logger.warning(
                "directory planning account deferred: tenant=%s bc=%s account=%s budget=%.3f tail=%s",
                plan.tenant_id,
                plan.bc_id,
                account,
                remaining_budget,
                not lock_conflict and not full_account_slice,
            )
            if not lock_conflict:
                break  # 尾部不足不惩罚该户，下轮从此处继续；前项仍在外层事务内。
        else:
            state["after"] = max(state["after"], account)
            deferred.pop(account, None)
            completed += 1
    logger.info(
        "directory planning batch yielded: tenant=%s bc=%s candidates=%d completed=%d remaining_ms=%.1f",
        plan.tenant_id,
        plan.bc_id,
        len(batch),
        completed,
        max(0, budget - (time.monotonic() - started)) * 1000,
    )
    state["deferred"] = deferred
    remaining = [a for a in accounts if a > state["after"] and a not in deferred]
    if not remaining and not deferred:
        plan.screening_progress = {
            k: v
            for k, v in plan.screening_progress.items()
            if k != DIRECTORY_PLAN_PROGRESS
        }
        plan.next_directory_at = _advance(anchor, now, planner.DIRECTORY_INTERVAL)
    else:
        plan.screening_progress = {
            **plan.screening_progress,
            DIRECTORY_PLAN_PROGRESS: state,
        }
        plan.next_directory_at = (
            now + DIRECTORY_PLAN_BATCH_INTERVAL
            if remaining
            else min(datetime.fromisoformat(at) for at in deferred.values())
        )


def _plan_one(
    database_engine,
    redis_client,
    key,
    now,
    deadline,
    hot,
    material,
    *,
    allow_backfill=True,
    one_step=False,
    time_budget=None,
    metrics=None,
    realtime_only=False,
    prefer_facts=False,
):
    # 每一阶段/批次自行提交；后续SQL或软超时不撤销已完成的筛查与入队。
    from app.modules.reporting.planning_steps import run_plan

    assert hot == material, "双实时事实必须使用相同周期"
    return run_plan(
        database_engine,
        redis_client,
        key,
        now=now,
        deadline=deadline,
        allow_backfill=allow_backfill,
        one_step=one_step,
        time_budget=time_budget,
        metrics=metrics,
        realtime_only=realtime_only,
        prefer_facts=prefer_facts,
    )


def _dispatch_report_jobs(
    session: Session,
    now: datetime,
    *,
    allow_backfill=True,
    redis_client=None,
    max_claims=None,
) -> int:
    from app.modules.sync_control.dispatcher import dispatch_due

    slots = REPORT_DISPATCH_WINDOW
    if redis_client is None:
        with Redis.from_url(settings.REDIS_URL) as redis:
            return dispatch_due(
                session,
                redis,
                now=now,
                physical_slots=slots,
                allow_backfill=allow_backfill,
                max_claims=max_claims,
            )
    return dispatch_due(
        session,
        redis_client,
        now=now,
        physical_slots=slots,
        allow_backfill=allow_backfill,
        max_claims=max_claims,
    )


@contextmanager
def _report_refill_after(database_engine, redis_client, job):
    completed = True
    try:
        yield
    except SoftTimeLimitExceeded, KeyboardInterrupt, SystemExit:
        completed = False
        raise
    finally:
        if completed:
            from app.modules.sync_control.costs import finish_current_cost

            finish_current_cost()
        if settings.ADS_SYNC_ENABLED:
            try:
                with diagnostics.stage("refill"):
                    now = datetime.now(UTC)
                    allow_backfill = planner.backfill_allowed(redis_client, now)
                    # 当前步骤提交后直接执行有锁本地领取；新消息仍写正常 outbox，
                    # 去掉先发 control 唤醒再领取的双跳，不在读取 Worker 内发送 broker。
                    # 后台事实与目录共用池，完成后按池公平顺序补位，避免空槽等Beat。
                    with diagnostics.stage("refill_dispatch"):
                        count = _dispatch_pending(
                            database_engine,
                            now,
                            allow_backfill=allow_backfill,
                            include_directories=job.execution_pool == "background",
                        )
                    diagnostics.counts(refill_jobs=count)
            except Exception:
                # 已提交的报表不能因补位消息失败改成失败；30 秒 Beat 会再次补位。
                logger.exception("report direct refill failed: job=%s", job.id)


def _dispatch_batch_hint(redis: Redis, key: str) -> tuple[str | None, int]:
    try:
        # redis同步/异步命令共用返回注解，此处明确使用同步客户端。
        observed = cast(bytes | str | None, redis.get(key))
        if observed is None:
            return None, DISPATCH_LARGE_COMMIT_BATCH
        value = observed.decode() if isinstance(observed, bytes) else observed
        limit = int(value.rsplit(":", 1)[1])
        return value, max(1, min(limit, DISPATCH_LARGE_COMMIT_BATCH))
    except RedisError, ValueError, IndexError, UnicodeError:
        # 提示不可用时保守提交单条，不能让辅助状态阻断业务领取。
        logger.warning("sync dispatch batch hint unavailable")
        return None, 1


def _update_dispatch_batch_hint(
    redis: Redis,
    key: str,
    *,
    observed: str | None = None,
    limit: int = 1,
) -> None:
    value = f"{uuid4().hex}:{limit}"
    try:
        if observed is None:
            # 每次超时都换代；并行旧批即使随后成功，也不能抹去新的降批。
            redis.set(key, value, ex=DISPATCH_BATCH_HINT_TTL)
        else:
            redis.eval(
                "if redis.call('get',KEYS[1]) == ARGV[1] then "
                "return redis.call('set',KEYS[1],ARGV[2],'EX',ARGV[3]) "
                "else return 0 end",
                1,
                key,
                observed,
                value,
                str(DISPATCH_BATCH_HINT_TTL),
            )
    except RedisError:
        # 此处在提交/回滚之后，提示写入失败不能改变已完成事务的计数。
        logger.warning("sync dispatch batch hint update unavailable")


def _dispatch_pending(
    database_engine: Engine,
    now: datetime,
    *,
    allow_backfill: bool = True,
    include_directories: bool = True,
    budget_seconds: float | None = None,
) -> int:
    from app.modules.sync_control.balance_demand import dispatch_balances_due
    from app.modules.sync_control.occupancy import reservation_transaction

    count = 0
    stop = time.monotonic() + (budget_seconds if budget_seconds is not None else 4)
    families: tuple[str, ...] = (
        ("report", "directory", "balance")
        if include_directories
        else ("report", "balance")
    )
    with Redis.from_url(settings.REDIS_URL) as redis:
        # 前一家族可能用完本轮总预算；跨唤醒轮换起点，不能每次都让目录/余额
        # 等在报表后面。共享游标仅分配扫描机会，不改变租约、额度或公平成本。
        # 两家族的Worker补位与三家族控制唤醒独立轮换，避免互相跳过某个起点。
        cursor_key = f"sync:dispatch-family:v1:{settings.TIKTOK_APP_ID}:{len(families)}"
        with redis.pipeline() as cursor:
            turn, _ = cursor.incr(cursor_key).expire(cursor_key, 86400).execute()
        start = (turn - 1) % len(families)
        families = families[start:] + families[:start]
        exhausted: set[str] = set()
        for round_index in range(8):
            current = 0
            for family in families:
                # 空家族/锁冲突在下一次唤醒重试；本轮不随另一类的每个续步重复扫描。
                if family in exhausted:
                    continue
                hint_key = f"sync:dispatch-batch:v1:{settings.TIKTOK_APP_ID}:{family}"
                observed, hint_limit = (
                    _dispatch_batch_hint(redis, hint_key)
                    if family != "balance"
                    else (None, DISPATCH_LARGE_COMMIT_BATCH)
                )
                remaining = stop - time.monotonic()
                if remaining <= 0:
                    return count
                # 首轮至多四个、后轮预算充足才用八个；上次超时的家族先提交
                # 单条，避免每次唤醒都撤销同一前缀。余额仍保留原批量。
                batch_limit = min(
                    hint_limit,
                    (
                        DISPATCH_LARGE_COMMIT_BATCH
                        if round_index > 0 and remaining > DISPATCH_BATCH_SAMPLE_SECONDS
                        else DISPATCH_COMMIT_BATCH
                    ),
                )
                if family != "balance" and remaining < DISPATCH_BATCH_SAMPLE_SECONDS:
                    # 尾部/规划余量只尝试单条，避免四条整批回滚；本轮不足
                    # 不代表下一次完整唤醒也只能领取一条。
                    batch_limit = 1
                try:
                    # 每类派发独立提交：后续查询耗尽总预算不能撤销前面已领取的
                    # 工作。租约、成本和outbox仍同事务；异常同步撤销Redis占位。
                    with (
                        reservation_transaction(redis),
                        Session(database_engine) as session,
                        session.begin(),
                        # Worker直接补位也必须受默认总预算约束，不能仅Beat有超时。
                        query_budget(session, seconds=remaining),
                    ):
                        if family == "report":
                            dispatched = _dispatch_report_jobs(
                                session,
                                now,
                                allow_backfill=allow_backfill,
                                max_claims=batch_limit,
                                redis_client=redis,
                            )
                        elif family == "directory":
                            dispatched = _dispatch_directories(
                                session,
                                now,
                                allow_backfill=allow_backfill,
                                max_claims=batch_limit,
                                redis_client=redis,
                            )
                        else:
                            dispatched = dispatch_balances_due(session, redis, now=now)
                        session.flush()
                    count += dispatched
                    current += dispatched
                    if (
                        observed is not None
                        and dispatched == batch_limit == hint_limit
                        and hint_limit < DISPATCH_LARGE_COMMIT_BATCH
                    ):
                        # 完整批提交后逐一恢复；CAS保证旧并行成功不能覆盖新降批。
                        _update_dispatch_batch_hint(
                            redis,
                            hint_key,
                            observed=observed,
                            limit=min(batch_limit + 1, DISPATCH_LARGE_COMMIT_BATCH),
                        )
                    if not dispatched:
                        exhausted.add(family)
                except (DBAPIError, QueryBudgetExceeded) as error:
                    if not _planning_budget_error(error):
                        raise
                    if (
                        family != "balance"
                        and remaining >= DISPATCH_BATCH_SAMPLE_SECONDS
                    ):
                        _update_dispatch_batch_hint(redis, hint_key)
                    logger.info(
                        "sync dispatch yielded: family=%s committed=%s", family, count
                    )
                    return count
            if not current:
                break
    return count


def _failed(session, job, code):
    job.status, job.error_code, job.finished_at = "FAILED", code, datetime.now(UTC)
    from app.modules.sync_control.schedule import record_report_outcome

    record_report_outcome(session, job)
    job.claim_token = job.claimed_until = None
    publish.mark_failed(
        session,
        tenant_id=job.tenant_id,
        advertiser_ids=job.advertiser_ids,
        kind=job.kind,
        start=job.start_date,
        end=job.end_date,
        error_code=code,
        requested_at=request_time(job),
    )


@diagnostics.summarize_job
def run_job_step(*, database_engine, redis_client, job_id, claim_token):
    from app.modules.sync_control.costs import execution_cost_context
    from app.modules.sync_control.occupancy import release_dispatch
    from app.modules.sync_control.schedule import report_endpoint

    with Session(database_engine) as session:
        job = session.get(ReportSyncJob, job_id)
        if job is None:
            return "FAILED"
        tenant_id, generation = job.tenant_id, job.claim_generation
        advertiser, endpoint = (
            job.advertiser_ids[0],
            report_endpoint(job.kind, job.progress),
        )
        valid_claim = job.claim_token == claim_token
        if job.kind == "operation_log" and valid_claim:
            from app.modules.sync_control.models import SyncCostLedger

            # 滚动发布时旧dispatcher可能已给CHECK签发CREATE票。冻结同代主收据
            # 的入口，不按执行后变化的phase释放；网关仍对真实发送接口独立准入。
            reserved_endpoint = session.exec(
                select(col(SyncCostLedger.physical_endpoint))
                .where(
                    col(SyncCostLedger.tenant_id) == tenant_id,
                    col(SyncCostLedger.report_job_id) == job_id,
                    col(SyncCostLedger.generation) == generation,
                )
                .order_by(
                    col(SyncCostLedger.reserved_units).desc(), col(SyncCostLedger.id)
                )
                .limit(1)
            ).first()
            endpoint = reserved_endpoint or endpoint
        # 旧消息可能跨代重放，而当前phase/ledger已经属于新票。仅在三个已知
        # 日志接口按旧token幂等清理queued占位；不触碰新票、平台次数或inflight。
        release_endpoints = (
            tuple(
                report_endpoint("operation_log", {"operation_log": {"phase": phase}})
                for phase in ("CREATE", "CHECK", "DOWNLOAD")
            )
            if job.kind == "operation_log" and not valid_claim
            else (endpoint,)
        )
    try:
        if not valid_claim:
            return "FAILED"
        with execution_cost_context(
            database_engine,
            tenant_id=tenant_id,
            execution_id=job_id,
            generation=generation,
            kind="report",
        ):
            return _run_job_step(
                database_engine=database_engine,
                redis_client=redis_client,
                job_id=job_id,
                claim_token=claim_token,
                reservation_endpoint=endpoint,
            )
    finally:
        try:
            # 发送前撤权也必须释放本消息的queued占位；旧token不会清新代，
            # 此函数只动dispatch键，不退还原平台已消费次数或正在发送的inflight。
            for release_endpoint in release_endpoints:
                release_dispatch(
                    redis_client,
                    reservation_id=claim_token,
                    app_scope=settings.TIKTOK_APP_ID,
                    endpoint=release_endpoint,
                    rate_group=release_endpoint,
                    tenant_id=tenant_id,
                    advertiser_id=advertiser,
                )
        except RedisError:
            logger.warning("report queued occupancy release deferred to expiry")


def _run_job_step(
    *, database_engine, redis_client, job_id, claim_token, reservation_endpoint=None
):
    queued_token = claim_token
    with diagnostics.stage("step_prepare"):
        with Session(database_engine, expire_on_commit=False) as session:
            job = session.get(ReportSyncJob, job_id, with_for_update=True)
            now = datetime.now(UTC)
            if job is None or job.claim_token != claim_token or job.status != "RUNNING":
                return "FAILED"
            diagnostics.set_job_scope(job.kind, len(job.advertiser_ids))
            if job.kind in {"material_day", "material_links"}:
                cancel_retired_report(session, job)
                session.commit()
                return "CANCELLED"

            # 旧积压消息不能越过有界派发窗口自行续租；只结束本条消息，
            # 业务运行仍留给扫描器按当前优先级重新领取，不记终态失败。
            if job.claimed_until is None or job.claimed_until <= now:
                return "FAILED"
            if not settings.ADS_SYNC_ENABLED:
                job.status, job.due_at = "WAITING", now + timedelta(minutes=1)
                from app.modules.sync_control.schedule import record_report_outcome

                record_report_outcome(session, job)
                job.claim_token = job.claimed_until = None
                with diagnostics.stage("job_commit"):
                    session.commit()
                diagnostics.committed("WAITING")
                return "WAITING"
            try:
                route = FrozenTikTokRoute.model_validate(job.frozen_route)
                if route.channel != "OFFICIAL_API":
                    raise DomainError("api_binding_required", "报表需要 API 绑定")
                for advertiser in job.advertiser_ids:
                    verify_route(
                        session,
                        context=_context(job),
                        route=route,
                        advertiser_id=advertiser,
                        capability="read",
                    )
            except DomainError as error:
                _failed(session, job, error.code)
                with diagnostics.stage("job_commit"):
                    session.commit()
                diagnostics.committed("FAILED")
                return "FAILED"
            workload = report_workload(session, job, now=now)
            before = job.request_count
            # 消费queued身份，旧消息不能越过同代执行围栏。
            job.claim_token = claim_token = uuid4()
            job.claimed_until = now + LEASE
            with diagnostics.stage("claim_commit"):
                session.commit()

    def factory():
        return Session(database_engine)

    with _report_refill_after(database_engine, redis_client, job):
        deadline = now + timedelta(seconds=170)
        try:
            with _reader_for(
                database_engine=database_engine,
                redis_client=redis_client,
                plan=job.model_copy(update={"claim_token": queued_token}),
                deadline=deadline,
                workload=workload,
                reservation_endpoint=reservation_endpoint,
            ) as reader:
                if reader is None:
                    raise DomainError("api_binding_required", "报表需要 API 绑定")
                result = execute_job(
                    factory,
                    job_id=job_id,
                    claim_token=claim_token,
                    reader=reader,
                )
        except AccountAdmissionDeferred as error:
            with factory() as session:
                current = session.get(ReportSyncJob, job_id, with_for_update=True)
                if current is not None and current.claim_token == claim_token:
                    from app.modules.sync_control.executions import ExecutionLost
                    from app.modules.sync_control.schedule import check_report_grant

                    try:
                        check_report_grant(session, current, queued_token)
                    except ExecutionLost:
                        return "FAILED"
                    current.status = "WAITING"
                    current.error_code = "admission_deferred"
                    current.due_at = now + timedelta(
                        milliseconds=max(1000, error.retry_after_ms)
                    )
                    from app.modules.sync_control.schedule import record_report_outcome

                    record_report_outcome(session, current)
                    current.claim_token = current.claimed_until = None
                    with diagnostics.stage("job_commit"):
                        session.commit()
                    diagnostics.committed("WAITING")
            return "WAITING"
        except DomainError as error:
            with factory() as session:
                current = session.get(ReportSyncJob, job_id, with_for_update=True)
                if current is not None and current.claim_token == claim_token:
                    from app.modules.sync_control.executions import ExecutionLost
                    from app.modules.sync_control.schedule import check_report_grant

                    try:
                        check_report_grant(session, current, queued_token)
                    except ExecutionLost:
                        return "FAILED"
                    _failed(session, current, error.code)
                    with diagnostics.stage("job_commit"):
                        session.commit()
                    diagnostics.committed("FAILED")
            return "FAILED"
        with diagnostics.stage("record_calls"):
            with factory() as session:
                current = session.get(ReportSyncJob, job_id)
                if current is not None:
                    # 计增量，不能在每次重试重复计入历史请求数。
                    planner.record_calls(
                        redis_client, now, max(0, current.request_count - before)
                    )
        return result


# 目录分页暂存只服务于本次运行的整批发布；运行发布或作废后不再被读取。
# 成功/作废页保留1小时；FAILED保留7天排查与人工恢复窗口，之后仅无活租约/待发布子桶才清页。
DIRECTORY_PAGE_RETENTION = timedelta(hours=1)
FAILED_DIRECTORY_PAGE_RETENTION = timedelta(days=7)
# 成功报表的规范事实已原子发布；临时响应不再承担查询或恢复职责。
REPORT_PAGE_RETENTION = timedelta(hours=1)
# 单批清理的运行数上限：暂存页含大 JSONB，分批各自提交，避免长事务。
DIRECTORY_PAGE_CLEANUP_RUNS = 2000
# 每分钟以10秒为续批预算，首轮每类仍有一次机会；短批各自提交，持续追赶暂存。
DIRECTORY_PAGE_CLEANUP_SECONDS = 10
CLEANUP_CURSOR_KEY = "reporting:cleanup:cursors:v2"
# 先按主键取有限候选，再判断保留期，避免无时间索引时 LIMIT 仍扫描全表。
CLEANUP_SCAN_ROWS = 1000
CLEANUP_BATCH_ROWS = 100
# 一分钟首屏会持续生成紧凑快照；只提高已过期快照子行的清理吞吐。
# 原始报表/目录大JSON页仍100条，保留期、2秒语句预算及拥塞缩批机制均不变。
CLEANUP_SNAPSHOT_BATCH_ROWS = 5000
# 无页父任务很小，单批放大到1000；大JSONB子页仍独立限制100页。
CLEANUP_JOB_BATCH_ROWS = 1000
CLEANUP_STATEMENT_MS = 2000
CLEANUP_LOCK_MS = 200


def _cleanup_default_batch(category: str) -> int:
    if category == "snapshots":
        return CLEANUP_SNAPSHOT_BATCH_ROWS
    return CLEANUP_JOB_BATCH_ROWS if category == "jobs" else CLEANUP_BATCH_ROWS


CLEANUP_CATEGORIES = (
    "covered_directories",
    "covered_reports",
    "material_pages",
    "jobs",
    "hours",
    "snapshots",
    "balances",
    "directory_superseded",
    "directory_pages",
)


def _supersede_stalled_directory_runs(now, *, candidate_ids=None, batch_limit=None):
    """仅清理已被同冻结范围完整覆盖的旧请求；局部刷新不能淘汰历史依赖。

    与执行/发布共用替代条件，租约有效的运行留给持有者结束，不抢改。
    """
    from app.modules.ads.directory_supersession import superseding_directory_predicate

    newer = aliased(AdDirectoryRun)
    candidates = select(col(AdDirectoryRun.id)).where(
        col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
        col(AdDirectoryRun.published_version).is_(None),
        col(AdDirectoryRun.claimed_until).is_(None)
        | (col(AdDirectoryRun.claimed_until) <= now),
        col(AdDirectoryRun.created_at) < now - DIRECTORY_PAGE_RETENTION,
        # 素材子运行覆盖冻结父组，不能以同账户其他组的发布淘汰。
        ~col(AdDirectoryRun.query).op("?")(MATERIAL_CHILD_KEY),
        select(newer.id)
        .where(
            superseding_directory_predicate(AdDirectoryRun, newer),
        )
        .exists(),
    )
    if candidate_ids is not None:
        candidates = candidates.where(col(AdDirectoryRun.id).in_(candidate_ids))
    return (
        update(AdDirectoryRun)
        .where(
            col(AdDirectoryRun.id).in_(
                candidates.limit(batch_limit or CLEANUP_BATCH_ROWS).with_for_update(
                    skip_locked=True
                )
            )
        )
        .values(
            status="STALE",
            error_code="superseded",
            completed_at=now,
            claim_token=None,
            claimed_until=None,
        )
    )


def _expired_directory_runs(now):
    from app.modules.sync_control.models import SyncExecutionScope

    finished = col(AdDirectoryRun.published_version).is_not(None) | col(
        AdDirectoryRun.status
    ).in_(["CANCELLED", "STALE", "FAILED"])
    return (
        select(col(AdDirectoryRun.id))
        .where(
            finished,
            col(AdDirectoryRun.claim_token).is_(None),
            col(AdDirectoryRun.claimed_until).is_(None)
            | (col(AdDirectoryRun.claimed_until) <= now),
            # 失败完成时间未知时保守保留，不能从创建时间倒推7天窗口。
            (col(AdDirectoryRun.status) != "FAILED")
            | (
                col(AdDirectoryRun.completed_at) < now - FAILED_DIRECTORY_PAGE_RETENTION
            ),
            ~select(col(SyncExecutionScope.id))
            .where(
                col(SyncExecutionScope.directory_run_id) == col(AdDirectoryRun.id),
                col(SyncExecutionScope.status) == "PENDING",
            )
            .exists(),
            # 被新运行取代的运行（superseded）不再需要排查窗口，立即清理。
            (col(AdDirectoryRun.error_code) == "superseded")
            | (
                func.coalesce(
                    col(AdDirectoryRun.completed_at), col(AdDirectoryRun.created_at)
                )
                < now - DIRECTORY_PAGE_RETENTION
            ),
            select(col(AdDirectoryPage.run_id))
            .where(col(AdDirectoryPage.run_id) == col(AdDirectoryRun.id))
            .exists(),
        )
        .limit(DIRECTORY_PAGE_CLEANUP_RUNS)
        .with_for_update(skip_locked=True)
    )


def _cleanup_primary_key(model):
    return tuple(model.__table__.primary_key.columns)


def _cleanup_candidates(session, model, *, cursor, limit):
    """主键窗口本身不附带过期条件；读取上限不受历史表大小或命中率影响。"""
    keys = _cleanup_primary_key(model)
    statement = select(*keys).order_by(*keys).limit(limit)
    if cursor:
        values = []
        for column, value in zip(keys, cursor, strict=True):
            try:
                kind = column.type.python_type
            except NotImplementedError:
                # SQLModel AutoString 不实现 python_type，实际存储类型由 impl 给出。
                # UUID 和带时区日期仍按原类型恢复，不能把整个复合游标当字符串。
                kind = column.type.impl.python_type
            values.append(
                datetime.fromisoformat(value) if kind is datetime else kind(value)
            )
        statement = statement.where(tuple_(*keys) > tuple_(*values))
    return keys, list(session.connection().execute(statement).all())


def _execution_cleanup_guard(model, execution_column, payload_key):
    from sqlalchemy import String, cast

    from app.modules.sync_control.models import SyncCostLedger, SyncExecutionScope

    pending = (
        select(col(SyncExecutionScope.id))
        .where(
            execution_column == model.id, col(SyncExecutionScope.status) == "PENDING"
        )
        .exists()
    )
    # 本函数仅保护分页清理，父任务由归档流程单独保留。成功执行的迟到消息
    # 只核对终态，不再读取分页，不能让七天的outbox留存拖住大响应正文。
    # 失败/取消执行仍保守等待消息证据压缩，避免丢失恢复线索。
    unpreserved_dispatch = (
        select(col(PendingDispatch.id))
        .where(
            col(PendingDispatch.tenant_id) == model.tenant_id,
            col(PendingDispatch.payload)[payload_key].astext == cast(model.id, String),
            col(PendingDispatch.payload)[payload_key].astext.is_not(None),
            col(PendingDispatch.compacted_at).is_(None),
        )
        .exists()
    )
    cost_column = getattr(SyncCostLedger, execution_column.key)
    unsettled_cost = (
        select(col(SyncCostLedger.id))
        .where(
            cost_column == model.id,
            (col(SyncCostLedger.receipt_version) > col(SyncCostLedger.settled_version))
            | (
                col(SyncCostLedger.closed).is_(False)
                & col(SyncCostLedger.abandoned_at).is_(None)
            ),
        )
        .exists()
    )
    # 未结算/未封存的未知成本仍是恢复证据；失效代数保守封存并结算后可回收。
    return (
        ~pending
        & ((model.status == "COMPLETE") | ~unpreserved_dispatch)
        & ~unsettled_cost
        & model.claim_token.is_(None)
    )


def _cleanup_category(session, *, category, now, cursors):
    from app.modules.sync_control.models import SyncExecutionScope

    if category == "covered_directories":
        from app.modules.sync_control.directory_retirement import (
            retire_covered_directories,
        )

        count, position = retire_covered_directories(
            session,
            now=now,
            cursor=cursors.get(category),
            scan_limit=cursors.get("_limits", {})
            .get(category, {})
            .get("scan", CLEANUP_SCAN_ROWS),
            batch_limit=cursors.get("_limits", {})
            .get(category, {})
            .get("batch", CLEANUP_BATCH_ROWS),
        )
        cursors[category] = position
        if position is None and not count:
            cursors["_exhausted"] = True
        return count

    if category == "covered_reports":
        from app.modules.sync_control.report_retirement import retire_covered_reports

        count, position = retire_covered_reports(
            session,
            now=now,
            cursor=cursors.get(category),
            scan_limit=cursors.get("_limits", {})
            .get(category, {})
            .get("scan", CLEANUP_SCAN_ROWS),
            batch_limit=cursors.get("_limits", {})
            .get(category, {})
            .get("batch", CLEANUP_BATCH_ROWS),
        )
        cursors[category] = position
        if position is None and not count:
            cursors["_exhausted"] = True
        return count

    # 摘要只由持久归档回收；平台小时金额是原始事实，不能当作30天缓存删除。
    # 保留旧游标类别为空操作，其他短期页及快照继续有界清理。
    if category in {"jobs", "balances", "hours"}:
        return 0

    expired_jobs = (
        (
            (col(ReportSyncJob.status) == "COMPLETE")
            & (col(ReportSyncJob.finished_at) < now - REPORT_PAGE_RETENTION)
            | col(ReportSyncJob.status).in_(["FAILED", "CANCELLED"])
            & (col(ReportSyncJob.finished_at) < now - timedelta(days=7))
        )
        & _execution_cleanup_guard(
            ReportSyncJob, col(SyncExecutionScope.report_job_id), "job_id"
        )
        & (
            col(ReportSyncJob.claimed_until).is_(None)
            | (col(ReportSyncJob.claimed_until) <= now)
        )
    )
    models = {
        "material_pages": (ReportSyncJob, expired_jobs),
        "snapshots": (QuerySnapshot, col(QuerySnapshot.expires_at) < now),
        "directory_superseded": (AdDirectoryRun, None),
        "directory_pages": (AdDirectoryRun, None),
    }
    model, condition = models[category]
    limits = cursors.get("_limits", {}).get(category, {})
    batch_limit = limits.get("batch", _cleanup_default_batch(category))
    # 分页从子表自己的主键窗口出发，避免为找到少量有页任务反复读无页父记录。
    # v2游标记录(job_id/run_id,page)，旧v1父表游标不复用；重扫不会越过保留策略。
    page_model = {
        "material_pages": ReportSyncPage,
        "directory_pages": AdDirectoryPage,
    }.get(category)
    keys, candidates = _cleanup_candidates(
        session,
        page_model or model,
        cursor=cursors.get(category),
        limit=limits.get("scan", CLEANUP_SCAN_ROWS),
    )
    if not candidates:
        cursors[category] = None
        cursors["_exhausted"] = True
        return 0
    parent_keys = _cleanup_primary_key(model)
    scope = (
        parent_keys[0].in_(list(dict.fromkeys(row[0] for row in candidates)))
        if page_model is not None
        else tuple_(*keys).in_(candidates)
    )
    eligible = select(*parent_keys).where(scope)
    if condition is not None:
        eligible = eligible.where(condition)
    has_pages = (
        select(col(ReportSyncPage.job_id))
        .where(col(ReportSyncPage.job_id) == col(ReportSyncJob.id))
        .exists()
    )
    if category == "material_pages":
        eligible = eligible.where(has_pages)
    eligible = eligible.limit(batch_limit).with_for_update(skip_locked=True)
    child_removed = 0
    if category == "material_pages":
        # DELETE计划可能重求值IN子查询；物化有限页键固定单条删除上限。
        page_keys = (
            select(col(ReportSyncPage.job_id), col(ReportSyncPage.page))
            .where(col(ReportSyncPage.job_id).in_(eligible))
            .limit(batch_limit)
            .with_for_update(skip_locked=True)
            .cte("expired_report_page_batch")
            .prefix_with("MATERIALIZED")
        )
        statement = delete(ReportSyncPage).where(
            tuple_(col(ReportSyncPage.job_id), col(ReportSyncPage.page)).in_(
                select(page_keys.c.job_id, page_keys.c.page)
            )
        )
    elif category == "directory_superseded":
        statement = _supersede_stalled_directory_runs(
            now, candidate_ids=[row[0] for row in candidates], batch_limit=batch_limit
        ).returning(col(AdDirectoryRun.id))
        identifiers = list(session.connection().execute(statement).scalars())
        from app.modules.sync_control.directory_demand import directory_outcome

        for identifier in identifiers:
            run = session.get(AdDirectoryRun, identifier, populate_existing=True)
            directory_outcome(session, run)
        if not identifiers:
            cursors[category] = [str(value) for value in candidates[-1]]
        return len(identifiers)
    elif category == "directory_pages":
        removed = cleanup_directory_pages(
            session,
            now=now,
            candidate_ids=[row[0] for row in candidates],
            batch_limit=batch_limit,
        )
        if not removed:
            cursors[category] = [str(value) for value in candidates[-1]]
        return removed
    else:
        if category == "snapshots":
            # 快照同样可能包含大量行；先排空有界子行，再删除父记录。
            rows = (
                select(col(QuerySnapshotRow.snapshot_id), col(QuerySnapshotRow.row_key))
                .where(col(QuerySnapshotRow.snapshot_id).in_(eligible))
                .limit(batch_limit)
                .with_for_update(skip_locked=True)
            )
            child_removed = (
                session.connection()
                .execute(
                    delete(QuerySnapshotRow).where(
                        tuple_(
                            col(QuerySnapshotRow.snapshot_id),
                            col(QuerySnapshotRow.row_key),
                        ).in_(rows)
                    )
                )
                .rowcount
            )
            eligible = eligible.where(
                ~select(col(QuerySnapshotRow.snapshot_id))
                .where(col(QuerySnapshotRow.snapshot_id) == col(QuerySnapshot.id))
                .exists()
            )
        statement = delete(model).where(tuple_(*keys).in_(eligible))
    removed = session.connection().execute(statement).rowcount
    # 仍有删除进展就复查同一窗口，以便一个大任务逐页排空；空窗口才前进。
    if child_removed:
        cursors["_child_progress"] = True
    if not removed and not child_removed:
        cursors[category] = [str(value) for value in candidates[-1]]
    return removed


def cleanup_store(session, *, now, categories=CLEANUP_CATEGORIES, cursors=None):
    """每类只执行一批；调用方负责提交，不把某类超时扩散到其余保留策略。"""
    cursors = {} if cursors is None else cursors
    result = dict.fromkeys(categories, 0)
    for category in categories:
        try:
            with session.begin_nested():
                session.connection().execute(
                    text(f"SET LOCAL statement_timeout = '{CLEANUP_STATEMENT_MS}ms'")
                )
                session.connection().execute(
                    text(f"SET LOCAL lock_timeout = '{CLEANUP_LOCK_MS}ms'")
                )
                result[category] = _cleanup_category(
                    session, category=category, now=now, cursors=cursors
                )
            # 短暂拥塞恢复后逐级放大，避免一次超时永久降低清理吞吐。
            limits = cursors.get("_limits", {}).get(category)
            if limits:
                limits["scan"] = min(CLEANUP_SCAN_ROWS, limits["scan"] * 2)
                maximum = _cleanup_default_batch(category)
                limits["batch"] = min(maximum, limits["batch"] * 2)
        except DBAPIError as error:
            # 数据库主动取消语句，savepoint 回滚后仍可处理其他类别；未知错误保留告警。
            if getattr(error.orig, "sqlstate", None) not in {"57014", "55P03"}:
                raise
            # 大窗口超时后下轮缩小重试，保持原游标；不能永久重跑同一超大批。
            limits = cursors.setdefault("_limits", {}).setdefault(category, {})
            limits["scan"] = max(1, limits.get("scan", CLEANUP_SCAN_ROWS) // 4)
            default_batch = _cleanup_default_batch(category)
            limits["batch"] = max(1, limits.get("batch", default_batch) // 4)
            cursors["_deferred"] = True
            if limits["scan"] == limits["batch"] == 1:
                logger.error(
                    "report cleanup requires single-row retry: category=%s cursor=%s",
                    category,
                    cursors.get(category),
                )
            logger.warning(
                "report cleanup batch deferred: category=%s sqlstate=%s",
                category,
                getattr(error.orig, "sqlstate", None),
            )
    return result


def cleanup_directory_pages(session, *, now, candidate_ids=None, batch_limit=None):
    """同时限制运行数与页数，单个大运行也不能产生无界 JSONB 删除。"""
    runs = _expired_directory_runs(now)
    if candidate_ids is not None:
        runs = runs.where(col(AdDirectoryRun.id).in_(candidate_ids))
    runs = runs.cte("expired_directory_runs").prefix_with("MATERIALIZED")
    pages = (
        select(col(AdDirectoryPage.run_id), col(AdDirectoryPage.page))
        .where(col(AdDirectoryPage.run_id).in_(select(runs.c.id)))
        .limit(batch_limit or CLEANUP_BATCH_ROWS)
        .with_for_update(skip_locked=True)
        .cte("expired_directory_page_batch")
        .prefix_with("MATERIALIZED")
    )
    return (
        session.connection()
        .execute(
            delete(AdDirectoryPage).where(
                tuple_(col(AdDirectoryPage.run_id), col(AdDirectoryPage.page)).in_(
                    select(pages.c.run_id, pages.c.page)
                )
            )
        )
        .rowcount
    )


@celery_app.task(name="reporting.plan_due", time_limit=30, soft_time_limit=25)
def plan_due(
    *, tenant_id: Any = None, actor_id: Any = None, payload: Any = None, dispatch=None
):
    if dispatch is not None:
        from app.jobs.tasks import guard_sync_dispatch

        if not guard_sync_dispatch(
            database_engine=engine,
            task_name="reporting.plan_due",
            tenant_id=tenant_id,
            actor_id=actor_id,
            payload=payload,
            dispatch=dispatch,
        ):
            return "IGNORED"
    del tenant_id, actor_id
    if payload == {"dispatch_only": True}:
        if not settings.ADS_SYNC_ENABLED:
            return 0
        now = datetime.now(UTC)
        with Redis.from_url(settings.REDIS_URL) as redis:
            allow_backfill = planner.backfill_allowed(redis, now)
        # 报表与目录共用轻量补位信号；只领取本地持久任务，不重做外部筛选。
        count = _dispatch_pending(engine, now, allow_backfill=allow_backfill)
        if count:
            from app.jobs.tasks import drain_dispatch

            # 领取事务已提交，再复用原一秒公平发送预算；新业务工作无需等第二个 tick。
            # 空补位不扫描 outbox，失败消息仍由原持久记录与 broker 退避恢复。
            drain_dispatch()
        return count
    with Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        retry=Retry(NoBackoff(), 0),
        retry_on_error=[],
    ) as redis:
        return plan_due_runs(database_engine=engine, redis_client=redis)


@celery_app.task(name="reporting.plan_continue", time_limit=30, soft_time_limit=25)
def plan_continue(*, tenant_id, actor_id, payload, dispatch=None):
    # 先验证既有严格正文，再验证消息身份/原30秒期限；逐BC仍核冻结route。
    _continuation_contract(payload)
    from app.jobs.tasks import guard_sync_dispatch

    if not guard_sync_dispatch(
        database_engine=engine,
        task_name="reporting.plan_continue",
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
        dispatch=dispatch,
    ):
        return "IGNORED"
    with Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        retry=Retry(NoBackoff(), 0),
        retry_on_error=[],
    ) as redis:
        return plan_due_runs(
            database_engine=engine, redis_client=redis, continuation=payload
        )


@celery_app.task(
    name="reporting.job_step",
    time_limit=200,
    soft_time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
)
def job_step(*, tenant_id, actor_id, payload, dispatch=None):
    from app.jobs.tasks import guard_sync_dispatch

    if not guard_sync_dispatch(
        database_engine=engine,
        task_name="reporting.job_step",
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
        dispatch=dispatch,
    ):
        return "IGNORED"
    return _consume_job_step(tenant_id=tenant_id, actor_id=actor_id, payload=payload)


def _consume_job_step(*, tenant_id, actor_id, payload):
    validate_dispatch_payload(payload)
    if set(payload) != {"job_id", "claim_id"}:
        raise DomainError("dispatch_payload_invalid", "报表任务参数无效")
    job_id, token = UUID(payload["job_id"]), UUID(payload["claim_id"])
    with Session(engine) as session:
        job = session.get(ReportSyncJob, job_id)
        if (
            job is None
            or str(job.tenant_id) != tenant_id
            or str(job.actor_id) != actor_id
        ):
            raise DomainError("action_forbidden", "报表任务操作者不匹配")
    with Redis.from_url(settings.REDIS_URL) as redis:
        return run_job_step(
            database_engine=engine, redis_client=redis, job_id=job_id, claim_token=token
        )


@celery_app.task(
    name="reporting.backfill_step",
    time_limit=200,
    soft_time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
)
def backfill_step(*, tenant_id, actor_id, payload, dispatch=None):
    from app.jobs.tasks import guard_sync_dispatch

    if not guard_sync_dispatch(
        database_engine=engine,
        task_name="reporting.backfill_step",
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
        dispatch=dispatch,
    ):
        return "IGNORED"
    # 相同执行合同与冻结权限，只把慢任务的物理执行容量隔离。
    return _consume_job_step(tenant_id=tenant_id, actor_id=actor_id, payload=payload)


@celery_app.task(name="reporting.cleanup_store", time_limit=60, soft_time_limit=55)
def cleanup_store_task():
    now = datetime.now(UTC)
    # 预算从第一次数据库操作前计算，每类轮流一批；任何批失败都不撤销已提交进展。
    deadline = time.monotonic() + DIRECTORY_PAGE_CLEANUP_SECONDS
    result = dict.fromkeys(CLEANUP_CATEGORIES, 0)
    cursor_key = CLEANUP_CURSOR_KEY
    with Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
    ) as redis:
        # 并发 Beat/人工重跑不交错推进游标；锁比60秒任务硬上限长，崩溃后自动释放。
        lock = redis.lock("reporting:cleanup:lock:v1", timeout=120, blocking=False)
        if not lock.acquire(blocking=False):
            return result
        try:
            return _cleanup_store_batches(
                redis, now=now, deadline=deadline, result=result, cursor_key=cursor_key
            )
        finally:
            lock.release()


def _cleanup_store_batches(redis, *, now, deadline, result, cursor_key):
    cursors = {
        key: json.loads(value)
        for key, value in redis.hgetall(cursor_key).items()
        if key in CLEANUP_CATEGORIES or key == "_limits"
    }
    completed = set()
    retries = dict.fromkeys(CLEANUP_CATEGORIES, 0)
    first_round = True
    while first_round or time.monotonic() < deadline:
        progressed = False
        for category in CLEANUP_CATEGORIES:
            if category in completed:
                continue
            # 首轮给每类一次独立机会，避免前几类耗尽软预算后固定饿死尾部类别。
            if not first_round and time.monotonic() >= deadline:
                return result
            previous = cursors.get(category)
            for marker in ("_child_progress", "_exhausted", "_deferred"):
                cursors.pop(marker, None)
            with Session(engine) as session, session.begin():
                batch = cleanup_store(
                    session, now=now, categories=(category,), cursors=cursors
                )
            # 只有数据库提交成功才推进恢复位置；Redis丢失只会安全重扫。
            with redis.pipeline(transaction=True) as pipe:
                pipe.hset(cursor_key, category, json.dumps(cursors.get(category)))
                pipe.hset(cursor_key, "_limits", json.dumps(cursors.get("_limits", {})))
                pipe.expire(cursor_key, 7 * 24 * 3600)
                pipe.execute()
            result[category] += batch[category]
            if cursors.pop("_exhausted", False):
                completed.add(category)
            if cursors.pop("_deferred", False):
                retries[category] += 1
                # 先轮转给其他类别，再在预算内缩批重试；不必等下一小时才试小批。
                if retries[category] >= 5:
                    completed.add(category)
                else:
                    progressed = True
            progressed |= (
                bool(batch[category])
                or bool(cursors.pop("_child_progress", False))
                or cursors.get(category) != previous
            )
        first_round = False
        if not progressed:
            break
    return result
