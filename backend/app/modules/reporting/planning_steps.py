"""有界规划事务：阶段游标与该批业务任务一起提交，失败不撤销其它阶段。"""

import copy
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting import planner
from app.modules.reporting.jobs import _CountingReader
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import AdSyncBc

logger = logging.getLogger(__name__)
KEY = "planning_steps_v1"
PHASES = (
    "screen_hot",
    "hot",
    "material",
    "empty_hot",
    "closeout",
    "screen_daily",
    "daily",
    "empty_daily",
    "history",
    "directory",
)
# 每个后台批之间给筛查一页和两对实时事实批；筛查不再等待40个阶段才续下一页。
# 无对应工作时跳过空位，仍由入口跨BC逐阶段公平轮转。
PHASE_ORDER = tuple(
    item
    for phase in PHASES
    if phase not in {"screen_hot", "hot", "material"}
    for item in ("screen_hot", "hot", "material", "hot", "material", phase)
)
BATCH = 64
SECONDS = 3
SCREEN_START_SECONDS = 2.2  # SDK至少2秒，另给页游标原子提交留0.2秒。
REALTIME_PHASES = frozenset({"screen_hot", "hot", "material"})


class ScreenYield(Exception):
    """一个平台页已保存，下次从下一页继续；调用方提交而非回滚。"""


def _new(now, **values):
    return {
        "at": now.isoformat(),
        "after": "",
        "size": BATCH,
        "deferred": {},
        "done": False,
        **values,
    }


def scope_digest(plan, accounts, timezones):
    return hashlib.sha256(
        json.dumps(
            [
                str(plan.actor_id),
                plan.frozen_route,
                [(a, timezones.get(a)) for a in accounts],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def schedule_scope_changes(session, plan):
    state = plan.screening_progress.get(KEY)
    if state:
        accounts = planner.owned_accounts(session, plan)
        timezones = planner._timezones(session, plan.tenant_id, accounts)
        if state.get("scope") != scope_digest(plan, accounts, timezones):
            # 新账户/时区/授权不等待下一周期；下一事务按新范围重建游标，保留规则相位。
            plan.screening_progress = {
                **plan.screening_progress,
                KEY: {**state, "pending": True},
            }


def _prepare(plan, accounts, now, timezones):
    scope = scope_digest(plan, accounts, timezones)
    state: dict[str, Any] = copy.deepcopy(plan.screening_progress.get(KEY) or {})
    if state.get("scope") != scope:
        # 新授权/归属不能沿用旧范围的完成游标。旧已提交任务仍保留其冻结身份。
        changed = bool(state)
        state = {"scope": scope, "phase": 0}
        if changed:
            state["force_live"] = True
            plan.next_daily_at = min(plan.next_daily_at, now)
            plan.next_directory_at = min(plan.next_directory_at, now)
    live = state.get("live")
    if (not live or live.get("done")) and (
        state.get("force_live") or min(plan.next_hot_at, plan.next_material_at) <= now
    ):
        forced = state.pop("force_live", False)
        hot_due = plan.next_hot_at <= now or forced
        state["live"] = {
            "anchor": plan.next_hot_at.isoformat(),
            "at": now.isoformat(),
            "hot_due": hot_due,
        }
        state["hot"] = _new(now, done=not hot_due)
        state["material"] = _new(now)
        if hot_due:
            if state.get("screen_hot", {}).get("done", True):
                state["screen_hot"] = _new(now)
            if state.get("closeout", {}).get("done", True):
                state["closeout"] = _new(now)
        if state.get("history", {}).get("done", True):
            state["history"] = _new(now)
    if (
        plan.next_daily_at <= now
        and state.get("screen_daily", {}).get("done", True)
        and "daily" not in state
    ):
        state["screen_daily"] = _new(now)
    return state


def _save(plan, state):
    state["pending"] = (
        any(
            isinstance(state.get(p), dict) and not state[p].get("done")
            for p in PHASES
            if p != "directory"
        )
        or bool(plan.screening_progress.get("directory_cycle_v1"))
        or bool(state.get("metadata_missing"))
        or bool(state.get("force_live"))
    )
    plan.screening_progress = {**plan.screening_progress, KEY: copy.deepcopy(state)}


def _candidate_accounts(state, phase, plan, accounts):
    if phase not in {"hot", "material"}:
        return accounts
    step = state.get(phase, {})
    covered = set(step.get("covered", ()))
    live = state.get("live", {})
    if live.get("clock_advanced") and step.get("at") == live.get("at"):
        # 时钟已推进证明本轮完整巡检曾完成，已有事实也已计入covered。
        # 晚筛查只补新活跃及原重试；旧版重置过的游标也不能再空扫整个账户池。
        # 完成后出现的新事实由下一live轮的完整巡检发现，不重新打开旧轮。
        needed = set(plan.last_active_advertiser_ids) | set(step.get("deferred", {}))
        return [a for a in accounts if a in needed and a not in covered]
    return [a for a in accounts if a not in covered]


def _batch(step, accounts, now):
    deferred = step.setdefault("deferred", {})
    for account in step.get("covered", ()):
        deferred.pop(account, None)
    retry = sorted(
        a
        for a, at in deferred.items()
        if a in accounts and datetime.fromisoformat(at) <= now
    )
    fresh = [a for a in accounts if a > step["after"] and a not in deferred]
    return (retry + fresh)[: max(1, step.get("size", BATCH))]


def _advance_batch(step, batch, accounts):
    deferred = step["deferred"]
    fresh = [a for a in batch if a not in deferred]
    if fresh:
        step["after"] = max(step["after"], *fresh)
    for account in batch:
        deferred.pop(account, None)
    step["size"] = min(BATCH, step.get("size", BATCH) * 2)
    step["done"] = not deferred and not any(a > step["after"] for a in accounts)


def _eligible(state, phase, plan, now, allow_backfill, accounts):
    accounts = _candidate_accounts(state, phase, plan, accounts)
    if (
        phase in {"screen_daily", "daily", "empty_daily", "history"}
        and not allow_backfill
    ):
        return False
    if phase == "directory":
        if (
            state.get("directory_retry")
            and datetime.fromisoformat(state["directory_retry"]) > now
        ):
            return False
        cursor = plan.screening_progress.get("directory_cycle_v1")
        return plan.next_directory_at <= now or bool(
            cursor
            and (
                any(
                    a > cursor["after"] and a not in cursor.get("deferred", {})
                    for a in accounts
                )
                or any(
                    datetime.fromisoformat(at) <= now
                    for at in cursor.get("deferred", {}).values()
                )
            )
        )
    step = state.get(phase)
    return bool(
        step
        and not step.get("done")
        and datetime.fromisoformat(step.get("retry_at", step["at"])) <= now
        and (
            phase.startswith("screen_")
            or _batch(step, accounts, now)
            or not step.get("deferred")
        )
    )


def _execute(
    session,
    plan,
    state,
    phase,
    accounts,
    now,
    database_engine,
    redis_client,
    budget,
):
    from app.modules.reporting import job_tasks

    del database_engine  # 平台读取由plan_step在此事务退出后执行。

    if phase == "directory":
        before = plan.screening_progress.get("directory_cycle_v1", {}).get("after", "")
        job_tasks._plan_directory_step(
            session, plan, accounts=accounts, now=now, budget=budget
        )
        cursor = plan.screening_progress.get("directory_cycle_v1")
        if cursor and cursor["after"] == before:
            state["directory_retry"] = plan.next_directory_at.isoformat()
        return
    step = state[phase]
    at = datetime.fromisoformat(step["at"])
    if phase.startswith("screen_"):
        days = 1 if phase == "screen_hot" else 32
        counted = None
        try:
            from contextlib import nullcontext

            from app.modules.sync_control.screening import ScreeningReader

            with nullcontext(ScreeningReader(plan)) as reader:
                counted = _CountingReader(reader)
                try:
                    active = job_tasks._active(
                        session,
                        counted,
                        plan,
                        accounts,
                        at,
                        days=days,
                        single_step=True,
                    )
                except ScreenYield:
                    return  # 已保存页游标，必须正常退出SAVEPOINT以便提交。
            step["done"] = True
            if days == 1:
                if state.get("metadata_missing"):
                    plan.active_screened_at = None
                plan.last_active_advertiser_ids = active
                state["empty_hot"] = _new(at, active=active)
                # 保留已检查的前缀和未扫尾部，只将新发现的前缀账户加入补偿重试。
                # covered只代表已规划活跃/已有事实，不能拿它当全部已检查账户。
                for name in ("hot", "material"):
                    fact = state.get(name)
                    if not fact or (name == "hot" and not state["live"]["hot_due"]):
                        continue
                    for account in set(active) - set(fact.get("covered", ())):
                        if account <= fact["after"]:
                            fact["deferred"].setdefault(account, now.isoformat())
                    candidates = _candidate_accounts(state, name, plan, accounts)
                    fact["done"] = not fact["deferred"] and not any(
                        a > fact["after"] for a in candidates
                    )
                if not all(state.get(p, {}).get("done") for p in ("hot", "material")):
                    state["live"]["done"] = False
                if now - at >= planner.HOT_INTERVAL:
                    # 老轮终于完成时保留其真实证据水位，立即再读当前轮，不能冒充刚筛查。
                    state["screen_hot"] = _new(now)
            else:
                state["daily"] = _new(at, active=active)
                state["empty_daily"] = _new(at, active=active)
        finally:
            if counted:
                planner.record_calls(redis_client, now, counted.count)
        return
    accounts = _candidate_accounts(state, phase, plan, accounts)
    batch = _batch(step, accounts, now)
    if not batch:
        if not step["deferred"]:
            step["done"] = True
        return
    if phase in {"hot", "material", "empty_hot"}:
        active = set(
            plan.last_active_advertiser_ids
        ) | planner.known_reporting_accounts(session, plan, accounts=batch, now=at)
        covered = set(step.get("covered", []))
        requested = sorted(active.intersection(batch) - covered)
        if phase == "hot":
            planner.plan_hot(session, plan, active=requested, now=at)
            step["covered"] = sorted(covered | set(requested))
        elif phase == "material":
            planner.plan_material(session, plan, active=requested, now=at)
            step["covered"] = sorted(covered | set(requested))
        else:
            active.update(step["active"])
            planner.mark_confirmed_empty(
                session,
                plan,
                inactive=sorted(set(batch) - active),
                window_fn=lambda tz: planner._hot_window(tz, at),
                now=at,
                observed_at=at,
            )
    elif phase == "closeout":
        planner.plan_day_closeout(session, plan, now=at, accounts=batch)
    elif phase == "daily":
        planner.plan_daily(
            session,
            plan,
            active_32d=sorted(set(batch).intersection(step["active"])),
            now=at,
        )
    elif phase == "empty_daily":
        planner.mark_confirmed_empty(
            session,
            plan,
            inactive=sorted(set(batch) - set(step["active"])),
            window_fn=planner.daily_window(at),
            now=at,
            observed_at=at,
        )
    elif phase == "history":
        ranges = step.setdefault("ranges", {})
        planner.plan_history(session, plan, now=at, accounts=batch, progress=ranges)
        for account in batch:
            if account in ranges:
                step["deferred"][account] = now.isoformat()
        batch = [a for a in batch if a not in ranges]
    _advance_batch(step, batch, accounts)
    if phase == "closeout" and step.get("done") and step["at"] != state["live"]["at"]:
        state[phase] = _new(datetime.fromisoformat(state["live"]["at"]))


def _finish_phase(plan, state, now, timezones, valid_accounts):
    """成功批与游标一次保存；失败批回滚后用同一收尾规则保存重试。"""
    from app.modules.reporting import job_tasks

    # 时钟只在广告和素材都完整规划后推进；相位锚点不使用失败重试时刻。
    if (
        state.get("live")
        and not state["live"].get("done")
        and all(state.get(p, {}).get("done") for p in ("hot", "material"))
    ):
        if not state["live"].get("clock_advanced"):
            # 规则相位可能在批次之间被调整，不能用旧冻结anchor覆盖当前配置。
            anchor = plan.next_hot_at
            plan.next_hot_at = plan.next_material_at = (
                job_tasks._advance(anchor, now, planner.HOT_INTERVAL)
                if state["live"]["hot_due"] and anchor <= now
                else anchor
            )
            state["live"]["clock_advanced"] = True
        at = datetime.fromisoformat(state["live"]["at"])
        if now - at >= planner.HOT_INTERVAL or (
            at != now
            and any(
                planner.local_today(tz, at) != planner.local_today(tz, now)
                for tz in {timezones[a] for a in valid_accounts}
            )
        ):
            # 旧轮已跨周期/当地午夜，完成后立即追赶；不能再等未来格点使早批超过SLO。
            # force_live只请求当前工作，保留已配置的下一规则相位。
            state["force_live"] = True
        state["live"]["done"] = True
    if "daily" in state and all(
        state.get(p, {}).get("done") for p in ("daily", "empty_daily")
    ):
        daily_at = datetime.fromisoformat(state["daily"]["at"])
        crossed_day = any(
            planner.local_today(tz, daily_at) != planner.local_today(tz, now)
            for tz in {timezones[a] for a in valid_accounts}
        )
        # 冻结窗口跨日完成时还有刚结束的一天未发现，先追赶再恢复每日时钟。
        plan.next_daily_at = now if crossed_day else planner._next_daily(now)
        del state["daily"]
        del state["empty_daily"]
    state["last_turn"] = datetime.now(UTC).isoformat()
    _save(plan, state)


def _plan_step(
    database_engine,
    redis_client,
    key,
    *,
    now,
    allow_backfill,
    budget=8,
    metrics=None,
    realtime_only=False,
    prefer_facts=False,
):
    """最多一个阶段事务；返回是否前进，空转阶段不会消耗后续BC的时间预算。"""
    started = monotonic()
    with (
        Session(database_engine) as session,
        session.begin(),
        query_budget(session, seconds=budget),
    ):
        plan = session.exec(
            select(AdSyncBc)
            .where(AdSyncBc.tenant_id == key[0], AdSyncBc.bc_id == key[1])
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if plan is None or not plan.enabled:
            return False
        from app.core.context import TenantContext
        from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
        from app.modules.accounts.routing import verify_route

        verify_route(
            session,
            context=TenantContext(
                tenant_id=plan.tenant_id, actor_id=plan.actor_id, role="operator"
            ),
            route=FrozenTikTokRoute.model_validate(plan.frozen_route),
            advertiser_id=None,
            capability="read",
        )
        previous = plan.screening_progress.get(KEY, {})
        if (
            previous.get("retry_at")
            and datetime.fromisoformat(previous["retry_at"]) > now
        ):
            _save(plan, {**previous, "last_turn": datetime.now(UTC).isoformat()})
            session.flush()
            return False
        timezones = planner.owned_account_timezones(session, plan)
        accounts = sorted(timezones)
        if not accounts:
            return False
        state = _prepare(plan, accounts, now, timezones)
        missing = []
        for account in accounts:
            try:
                timezone = timezones.get(account)
                if timezone is None:
                    raise ValueError("account timezone is missing")
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError, ValueError, TypeError:
                missing.append(account)
        state["metadata_missing"] = missing
        if missing:
            plan.active_screened_at = None
        missing_set = set(missing)
        valid_accounts = [a for a in accounts if a not in missing_set]
        metadata_done = monotonic()
        phase = None
        phase_cursor = state["phase"]
        available = max(0, budget - (monotonic() - started))
        # 每页独立提交；前一页快时可用真实余量续页，不必再等一个完整入口片。
        can_screen = available >= SCREEN_START_SECONDS
        facts_ready = prefer_facts and any(
            _eligible(state, name, plan, now, allow_backfill, valid_accounts)
            for name in ("hot", "material")
        )
        if (
            state.get("screen_waiting_since")
            and can_screen
            and not facts_ready
            and _eligible(
                state, "screen_hot", plan, now, allow_backfill, valid_accounts
            )
        ):
            phase = "screen_hot"
        if phase is None:
            offsets = list(range(len(PHASE_ORDER)))
            if facts_ready:
                # 一个外部页后先服务事实，避免另一慢页在入口尾部吞掉全部事实机会。
                offsets.sort(
                    key=lambda offset: (
                        PHASE_ORDER[(phase_cursor + offset) % len(PHASE_ORDER)]
                        not in {"hot", "material"}
                    )
                )
            for offset in offsets:
                index = (phase_cursor + offset) % len(PHASE_ORDER)
                candidate = PHASE_ORDER[index]
                if realtime_only and candidate not in REALTIME_PHASES:
                    continue
                if not _eligible(
                    state,
                    candidate,
                    plan,
                    now,
                    allow_backfill,
                    accounts if candidate == "directory" else valid_accounts,
                ):
                    continue
                if candidate.startswith("screen_") and not can_screen:
                    if candidate == "screen_hot":
                        state.setdefault("screen_waiting_since", now.isoformat())
                    continue
                phase = candidate
                state["phase"] = (index + 1) % len(PHASE_ORDER)
                break
        if phase is None:
            state["last_turn"] = datetime.now(UTC).isoformat()
            _save(plan, state)
            session.flush()
            return False
        waiting_since = state.get("screen_waiting_since")
        if phase == "screen_hot":
            state.pop("screen_waiting_since", None)
        elif facts_ready and _eligible(
            state, "screen_hot", plan, now, allow_backfill, valid_accounts
        ):
            # 为事实让位同样保留等待年龄，不能让它下一轮排在已读过页的BC之后。
            state.setdefault("screen_waiting_since", now.isoformat())
        before = copy.deepcopy(state)
        full_budget = 8 if phase.startswith("screen_") else SECONDS
        budget = min(available, full_budget)
        read_budget = budget - 0.2 if phase.startswith("screen_") else budget
        work_started = monotonic()
        completed = True
        try:
            with session.begin_nested(), query_budget(session, seconds=budget):
                _execute(
                    session,
                    plan,
                    state,
                    phase,
                    accounts if phase == "directory" else valid_accounts,
                    now,
                    database_engine,
                    redis_client,
                    read_budget,
                )
                work_seconds = monotonic() - work_started
                _finish_phase(plan, state, now, timezones, valid_accounts)
                session.flush()
        except (
            DBAPIError,
            QueryBudgetExceeded,
            DomainError,
            AccountAdmissionDeferred,
        ) as error:
            # 只有当前批次回滚；保存缩批/重试与阶段轮转，慢阶段不会挡住其他工作。
            completed = False
            work_seconds = monotonic() - work_started
            state = before
            delay = timedelta(seconds=5)
            if isinstance(error, DBAPIError) and getattr(
                error.orig, "sqlstate", None
            ) not in {"57014", "55P03", "40P01"}:
                raise
            lock_conflict = isinstance(error, DBAPIError) and getattr(
                error.orig, "sqlstate", None
            ) in {"55P03", "40P01"}
            if isinstance(error, AccountAdmissionDeferred):
                delay = timedelta(milliseconds=max(1000, error.retry_after_ms))
            elif isinstance(error, DomainError):
                if (
                    error.code == "report_permission_denied"
                    or error.code == "tiktok_response_error"
                    and any(v in error.message.lower() for v in ("permission", "权限"))
                ):
                    plan.bc_report_supported = False
                    delay = timedelta(minutes=1)
                elif error.code not in {
                    "read_deadline_exceeded",
                    "tiktok_call_deadline_exceeded",
                }:
                    plan.disabled_reason = error.code
                    delay = timedelta(minutes=5)
            deadline_error = (
                isinstance(error, QueryBudgetExceeded)
                or (
                    isinstance(error, DBAPIError)
                    and getattr(error.orig, "sqlstate", None) == "57014"
                )
                or (
                    isinstance(error, DomainError)
                    and error.code
                    in {
                        "read_deadline_exceeded",
                        "tiktok_call_deadline_exceeded",
                    }
                )
            )
            if deadline_error and budget < full_budget - 0.1:
                # 入口余量不足不证明64户批次本身过慢；保留当前轮位置，下次给完整片。
                state["phase"] = phase_cursor
                if phase == "screen_hot":
                    # 短片失败不能刷新等待年龄，否则总在尾部失败的BC会永久让位。
                    state.setdefault(
                        "screen_waiting_since", waiting_since or now.isoformat()
                    )
            elif phase == "directory":
                plan.next_directory_at = now + delay
                state["directory_retry"] = (now + delay).isoformat()
            else:
                step = state[phase]
                step["retry_at"] = (now + delay).isoformat()
                if phase == "screen_daily":
                    plan.next_daily_at = now + delay
                if not phase.startswith("screen_") and not lock_conflict:
                    batch = _batch(
                        step,
                        _candidate_accounts(state, phase, plan, valid_accounts),
                        now,
                    )
                    step["size"] = max(1, step.get("size", BATCH) // 2)
                    if len(batch) == 1:
                        step["deferred"][batch[0]] = (
                            now + timedelta(minutes=5)
                        ).isoformat()
            logger.warning(
                "report planning phase deferred: bc=%s phase=%s error=%s code=%s budget=%.3f",
                plan.bc_id,
                phase,
                type(error).__name__,
                getattr(error, "code", None)
                or getattr(getattr(error, "orig", None), "sqlstate", None),
                budget,
            )
            _finish_phase(plan, state, now, timezones, valid_accounts)
            # 错误状态也必须在外层总预算内写入；不能留给退出预算后的自动flush。
            # 若余量已耗尽，当前阶段回滚，由下一轮沿旧游标重试。
            session.flush()
    total_seconds = monotonic() - started
    metadata_seconds = metadata_done - started
    if metrics is not None:
        if phase.startswith("screen_"):
            metrics["screen_attempts"] = metrics.get("screen_attempts", 0) + 1
            metrics["facts_since_screen"] = 0
        elif phase in {"hot", "material"}:
            metrics["facts_since_screen"] = metrics.get("facts_since_screen", 0) + 1
        outcome = "completed" if completed else "deferred"
        metrics[outcome] = metrics.get(outcome, 0) + 1
        for name, value in (
            ("metadata_s", metadata_seconds),
            ("work_s", work_seconds),
            ("stage_s", total_seconds),
        ):
            metrics[name] = metrics.get(name, 0) + value
    logger.info(
        "report planning phase finished: bc=%s phase=%s completed=%s owned=%d metadata_ms=%.1f work_ms=%.1f total_ms=%.1f budget=%.3f",
        key[1],
        phase,
        completed,
        len(accounts),
        metadata_seconds * 1000,
        work_seconds * 1000,
        total_seconds * 1000,
        budget,
    )
    return True


def plan_step(database_engine, redis_client, key, **options):
    from app.modules.reporting import job_tasks
    from app.modules.sync_control.screening import (
        ScreeningReadRequired,
        screening_responses,
    )

    responses = {}
    try:
        with screening_responses(responses):
            return _plan_step(database_engine, redis_client, key, **options)
    except ScreeningReadRequired as request:
        # 原阶段全部回滚退出；没有业务session/行锁跨过SDK请求。
        counted = None
        try:
            with job_tasks._reader_for(
                database_engine=database_engine,
                redis_client=redis_client,
                plan=request.plan,
                deadline=datetime.now(UTC)
                + timedelta(seconds=max(1, options.get("budget", 8) - 0.2)),
            ) as reader:
                if reader is None:
                    return False
                counted = _CountingReader(reader)
                response = getattr(counted, request.method)(**request.arguments)
        except (DomainError, AccountAdmissionDeferred) as error:
            # 网关建立同样可能额度退避；回放进原阶段保存筛查退避，仍让已知活跃需求继续。
            response = error
        finally:
            if counted is not None:
                planner.record_calls(redis_client, options["now"], counted.count)
        responses[request.fingerprint] = response
    try:
        # 重新锁计划、重算同一phase；状态或冻结授权改变会生成不同指纹，旧回包作废。
        with screening_responses(responses):
            return _plan_step(database_engine, redis_client, key, **options)
    except ScreeningReadRequired:
        return False


def run_plan(
    database_engine,
    redis_client,
    key,
    *,
    now,
    deadline,
    allow_backfill,
    one_step=False,
    time_budget=None,
    metrics=None,
    realtime_only=False,
    prefer_facts=False,
):
    # 测试/恢复可传历史逻辑时钟；运行预算始终使用单调时钟而非业务日期。
    remaining = (deadline - now).total_seconds() if time_budget is None else time_budget
    stop = monotonic() + max(0, min(10, remaining))
    advanced = False
    while stop - monotonic() >= 2:
        try:
            worked = plan_step(
                database_engine,
                redis_client,
                key,
                now=now,
                allow_backfill=allow_backfill,
                budget=max(0, stop - monotonic()),
                metrics=metrics,
                realtime_only=realtime_only,
                prefer_facts=prefer_facts,
            )
        except SoftTimeLimitExceeded:
            # 当前事务已退出并回滚；之前各批次仍已提交，下轮继续持久游标。
            raise
        advanced |= worked
        if one_step or not worked:
            break
    return advanced
