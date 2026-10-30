"""报表同步计划：每个已绑定官方 API 连接的 BC 一行，授权完成即自动建立。

一个账户若被多个 BC 授权，只由 bc_id 字典序最小的 BC 计划拉取，避免重复调用；
数据按账户存放，各 BC 的可见性由查询层按当前授权判断。
"""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, or_, text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import ReportReader
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import AdvertiserAccount, DiscoveryRun
from app.modules.accounts.routing import freeze_route
from app.modules.reporting.jobs import enqueue_job, enqueue_jobs
from app.modules.reporting.store_models import (
    AdSyncAccount,
    AdSyncBc,
    ReportSyncJob,
    ReportSyncState,
)
from app.modules.tenants.permissions import require_tenant

HOT_INTERVAL = timedelta(minutes=15)
# 发布后目录年龄约为 15k−e 分钟，e 为上次刷新耗时。
# 阈值放在相邻发布年龄的中间，配合包含等号的到期判断，e≤7.5 分钟时不会漂移。
REFRESH_MARGIN = HOT_INTERVAL / 2
RULE_SYNC_LEAD = timedelta(minutes=3)
MATERIAL_INTERVAL = timedelta(minutes=15)
HOUR_INTERVAL = timedelta(minutes=30)  # 小时趋势按需缓存，不占当前日止损热刷容量。
DIRECTORY_INTERVAL = timedelta(hours=3)
REFRESH_DAYS = 32
DAILY_AT = time(12, 30)  # 平台 UTC 12:00 修正数据之后
ROWS_PER_BATCH_TARGET = 15_000  # 预估行数超过即减少合并账户（平台上限 2 万）


def local_today(timezone: str | None, now: datetime) -> date:
    if timezone is None:
        raise ValueError("account timezone is missing")
    return now.astimezone(ZoneInfo(timezone)).date()


def _next_daily(now: datetime) -> datetime:
    target = datetime.combine(now.date(), DAILY_AT, UTC)
    return target if target > now else target + timedelta(days=1)


def _actor(
    session: Session, tenant_id: UUID, bc_id: str, connection_id: UUID
) -> UUID | None:
    """后台身份：该 BC 最近一次完成账户发现、且当前仍有租户权限的操作者。

    平台管理员无需成员关系即可代租户发现账户，可直接作为后台身份；已被移出
    租户或停用的成员跳过，回退到更早一次仍有权限者的发现记录。
    """
    runs = session.exec(
        select(DiscoveryRun)
        .where(
            DiscoveryRun.tenant_id == tenant_id,
            DiscoveryRun.connection_id == connection_id,
            # Official API account discovery is connection-wide, so its
            # completed run intentionally has no BC scope.  Keep accepting
            # exact-BC runs for older/MCP-shaped records, but never cross the
            # tenant or connection boundary above.
            or_(col(DiscoveryRun.bc_id) == bc_id, col(DiscoveryRun.bc_id).is_(None)),
            DiscoveryRun.status == "COMPLETE",
        )
        .order_by(
            col(DiscoveryRun.completed_at).desc().nulls_last(),
            col(DiscoveryRun.created_at).desc(),
        )
    ).all()
    checked: set[UUID] = set()
    for run in runs:
        if run.actor_id in checked:
            continue
        checked.add(run.actor_id)
        try:
            require_tenant(
                session, actor_id=run.actor_id, tenant_id=tenant_id, action="read"
            )
        except DomainError:
            continue
        actor: UUID = run.actor_id
        return actor
    return None


def ensure_bc_plans(
    session: Session, *, now: datetime, tenant_id: UUID | None = None
) -> int:
    enabled = 0
    scope_tenant = tenant_id
    binding_query = select(BCConnectionBinding).where(
        BCConnectionBinding.status == "ACTIVE"
    )
    plan_query = select(AdSyncBc)
    if scope_tenant is not None:
        binding_query = binding_query.where(
            BCConnectionBinding.tenant_id == scope_tenant
        )
        plan_query = plan_query.where(AdSyncBc.tenant_id == scope_tenant)
    bindings = session.exec(binding_query).all()
    # 同 BC 有多个 API 绑定时稳定选择一个，不能受数据库返回顺序影响。
    api = {
        (b.tenant_id, b.bc_id): b
        for b in sorted(bindings, key=lambda b: str(b.connection_id), reverse=True)
        if b.kind == "OFFICIAL_API"
    }
    existing = session.exec(plan_query).all()
    for tenant_id, bc_id in {(b.tenant_id, b.bc_id) for b in bindings} | {
        (p.tenant_id, p.bc_id) for p in existing
    }:
        plan = session.get(AdSyncBc, (tenant_id, bc_id))
        binding = api.get((tenant_id, bc_id))
        if binding is None:
            if plan is not None:
                plan.enabled, plan.disabled_reason = False, "api_binding_required"
            continue
        actor = _actor(session, tenant_id, bc_id, binding.connection_id) or (
            plan.actor_id if plan else None
        )
        if actor is None:
            continue  # 尚无完成的账户发现：等待授权流程完成
        context = TenantContext(tenant_id=tenant_id, actor_id=actor, role="operator")
        try:
            route = freeze_route(
                session,
                context=context,
                bc_id=bc_id,
                connection_id=binding.connection_id,
            )
        except DomainError as error:
            if plan is not None:
                plan.enabled, plan.disabled_reason = False, error.code
            continue
        route_json = route.model_dump(mode="json")
        if plan is None:
            plan = AdSyncBc(
                tenant_id=tenant_id,
                bc_id=bc_id,
                frozen_route=route_json,
                actor_id=actor,
                next_hot_at=now,
                next_material_at=now,
                next_directory_at=now,
                # 新授权的 BC 立即回刷近 32 天；历史回补只覆盖 32 天之前，
                # 若等到下一个每日时点，中间这段数据会一直为空。
                next_daily_at=now,
            )
            session.add(plan)
        plan.frozen_route, plan.actor_id, plan.enabled, plan.disabled_reason = (
            route_json,
            actor,
            True,
            None,
        )
        plan.updated_at = now
        enabled += 1
    session.flush()
    _refresh_owners(session, tenant_id=scope_tenant)
    for plan in session.exec(plan_query.where(col(AdSyncBc.enabled).is_(True))).all():
        align_rule_sync_phase(session, plan, now=now)
        from app.modules.reporting.planning_steps import schedule_scope_changes

        schedule_scope_changes(session, plan)
    return enabled


def align_rule_sync_phase(session: Session, plan: AdSyncBc, *, now: datetime) -> None:
    """规则提前三分钟取数，停用后恢复本 BC 原相位，避免所有租户永远扎堆。"""
    from app.modules.reporting.directory_refresh import rule_accounts

    accounts = owned_accounts(session, plan)
    has_rules = bool(
        rule_accounts(
            session, tenant_id=plan.tenant_id, bc_id=plan.bc_id, accounts=accounts
        )
    )
    # 复用计划已有 JSON 记录临时相位转换；账户筛选游标只删除自己的键，不会丢失此锚点。
    key = "rule_sync_original_phase"
    original = plan.screening_progress.get(key)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    anchor = epoch - RULE_SYNC_LEAD
    if has_rules:
        if original is None:
            plan.screening_progress = {
                **plan.screening_progress,
                key: plan.next_hot_at.isoformat(),
            }
            # 首轮/已到期任务立即可规划；尚未到期的 BC 提前到下一个规则准备刻度。
            steps, remainder = divmod(now - anchor, HOT_INTERVAL)
            if plan.next_hot_at > now and remainder:
                steps += 1
            target = anchor + steps * HOT_INTERVAL
        else:
            # 调度晚到仍沿网格推进；意外规划失败的退避不得把相位永久改成失败时刻。
            due = plan.next_hot_at
            steps, remainder = divmod(due - anchor, HOT_INTERVAL)
            target = anchor + (steps + bool(remainder)) * HOT_INTERVAL
        plan.next_hot_at = plan.next_material_at = target
    elif original is not None:
        unruled = datetime.fromisoformat(original)
        plan.next_hot_at = plan.next_material_at = (
            unruled + ((now - unruled) // HOT_INTERVAL + 1) * HOT_INTERVAL
        )
        plan.screening_progress = {
            k: v for k, v in plan.screening_progress.items() if k != key
        }


def _refresh_owners(session, *, tenant_id=None) -> None:
    # 先排除稳定归属，避免每轮锁住全池；ON CONFLICT的WHERE仍用于并发变化防线。
    session.connection().execute(
        text("""
        WITH desired AS (
          SELECT a.tenant_id, a.advertiser_id, MIN(a.bc_id) AS owner_bc_id
          FROM bc_account_access a JOIN ad_sync_bc p ON p.tenant_id=a.tenant_id AND p.bc_id=a.bc_id AND p.enabled
            AND a.connection_id::text = p.frozen_route->>'connection_id'
          WHERE (CAST(:tenant AS uuid) IS NULL OR a.tenant_id=CAST(:tenant AS uuid)) AND a.in_bc AND a.authorized AND a.active
          GROUP BY a.tenant_id, a.advertiser_id
        )
        INSERT INTO ad_sync_account (tenant_id, advertiser_id, owner_bc_id)
        SELECT d.tenant_id,d.advertiser_id,d.owner_bc_id FROM desired d
        LEFT JOIN ad_sync_account current ON current.tenant_id=d.tenant_id
          AND current.advertiser_id=d.advertiser_id
        WHERE current.owner_bc_id IS DISTINCT FROM d.owner_bc_id
        ON CONFLICT (tenant_id, advertiser_id) DO UPDATE SET owner_bc_id = EXCLUDED.owner_bc_id
        WHERE ad_sync_account.owner_bc_id IS DISTINCT FROM EXCLUDED.owner_bc_id"""),
        {"tenant": tenant_id},
    )
    session.connection().execute(
        text("""
        DELETE FROM ad_sync_account s WHERE (CAST(:tenant AS uuid) IS NULL OR s.tenant_id=CAST(:tenant AS uuid)) AND NOT EXISTS (
          SELECT 1 FROM bc_account_access a JOIN ad_sync_bc p ON p.tenant_id=a.tenant_id AND p.bc_id=a.bc_id AND p.enabled
          AND a.connection_id::text = p.frozen_route->>'connection_id'
          WHERE a.tenant_id=s.tenant_id AND a.advertiser_id=s.advertiser_id AND a.in_bc AND a.authorized AND a.active)"""),
        {"tenant": tenant_id},
    )


def account_owners(session, *, tenant_id: UUID) -> dict[str, str]:
    return dict(
        session.exec(
            select(AdSyncAccount.advertiser_id, AdSyncAccount.owner_bc_id).where(
                AdSyncAccount.tenant_id == tenant_id
            )
        ).all()
    )


def owned_accounts(session, plan: AdSyncBc) -> list[str]:
    return sorted(
        session.exec(
            select(AdSyncAccount.advertiser_id).where(
                AdSyncAccount.tenant_id == plan.tenant_id,
                AdSyncAccount.owner_bc_id == plan.bc_id,
            )
        ).all()
    )


def owned_account_timezones(session, plan: AdSyncBc) -> dict[str, str | None]:
    """阶段事务内一次读取归属与时区；缺失元数据仍保留账户，交由规划隔离。"""
    # 先取得本BC的精确集合，再一次查询元数据。不要把每个账户参数化为
    # LATERAL内层：稀疏/刚扩容的租户统计会误选(tenant,name,id)索引并重复扫前缀。
    accounts = owned_accounts(session, plan)
    values = dict.fromkeys(accounts)
    if accounts:
        values.update(
            session.execute(
                text("""
                /* planning_owned_metadata */
                SELECT advertiser_id,timezone FROM advertiser_account
                WHERE tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[]))
                """),
                {"tenant": plan.tenant_id, "accounts": accounts},
            ).all()
        )
    # 只供本阶段事务内的子查询复用，提交/回滚后的新事务必须重新读取授权范围。
    session.info["reporting_phase_timezones"] = (
        session.get_transaction(),
        plan.tenant_id,
        values,
    )
    return values


def due_plans(
    session, *, now: datetime, limit: int = 50, realtime_only: bool = False
) -> list[AdSyncBc]:
    live_condition = text("""
      (COALESCE((screening_progress->'planning_steps_v1'->>'retry_at')::timestamptz,
                '-infinity'::timestamptz) <= :live_now)
      AND (next_hot_at <= :live_now OR next_material_at <= :live_now
        OR screening_progress->'planning_steps_v1'->>'force_live' = 'true'
        OR EXISTS (
          SELECT 1 FROM jsonb_each(COALESCE(screening_progress->'planning_steps_v1','{}'::jsonb)) p
          WHERE p.key IN ('screen_hot','hot','material')
            AND p.value->>'at' IS NOT NULL
            AND COALESCE(p.value->>'done','false') != 'true'
            AND COALESCE((p.value->>'retry_at')::timestamptz,'-infinity'::timestamptz) <= :live_now
        ))
    """).bindparams(live_now=now)
    return list(
        session.exec(
            select(AdSyncBc)
            .where(
                col(AdSyncBc.enabled).is_(True),
                *([live_condition] if realtime_only else []),
                exists().where(
                    col(AdSyncAccount.tenant_id) == col(AdSyncBc.tenant_id),
                    col(AdSyncAccount.owner_bc_id) == col(AdSyncBc.bc_id),
                ),
                (AdSyncBc.next_hot_at <= now)
                | (AdSyncBc.next_material_at <= now)
                | (AdSyncBc.next_daily_at <= now)
                | (AdSyncBc.next_directory_at <= now)
                | (
                    col(AdSyncBc.screening_progress)["planning_steps_v1"][
                        "pending"
                    ].astext
                    == "true"
                ),
            )
            .order_by(
                # 只提升确实因入口余量不足而未读到下一页的BC，一页成功或真实退避即清。
                col(AdSyncBc.screening_progress)["planning_steps_v1"][
                    "screen_waiting_since"
                ]
                .astext.asc()
                .nulls_last(),
                col(AdSyncBc.screening_progress)["planning_steps_v1"]["last_turn"]
                .astext.asc()
                .nulls_first(),
                col(AdSyncBc.next_hot_at),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )


def screen_active(
    reader: ReportReader, *, bc_id: str, start: date, end: date, candidates: set[str]
) -> set[str]:
    """BC 报表一次返回整个 BC 下账户的消耗；只保留本计划负责的账户。"""
    active, page = set(), 1
    while True:
        result = reader.bc_spend(bc_id=bc_id, start=start, end=end, page=page)
        for row in result.rows:
            advertiser = str((row.get("dimensions") or {}).get("advertiser_id"))
            spend = (row.get("metrics") or {}).get("spend")
            if (
                advertiser in candidates
                and spend not in (None, "-")
                and float(spend) > 0
            ):
                active.add(advertiser)
        if result.next_page is None:
            return active
        page = result.next_page


def batch_accounts(
    session, *, tenant_id, advertiser_ids: Sequence[str], days: int
) -> list[list[str]]:
    """按近7天已发布广告状态的最大行数估量；未知账户保留默认值及正常分页。"""
    sizes = dict(
        session.connection()
        .execute(
            text("""
        SELECT advertiser_id, MAX(row_count) FROM report_sync_state
          WHERE tenant_id=:t AND advertiser_id = ANY(:a)
            AND report_kind='ad_day' AND last_success_at IS NOT NULL
            AND stat_date >= CURRENT_DATE - 7
          GROUP BY advertiser_id"""),
            {"t": tenant_id, "a": list(advertiser_ids)},
        )
        .all()
    )
    batches, current, load = [], [], 0
    for advertiser in sorted(advertiser_ids):
        estimate = sizes.get(advertiser, 100) * days
        if current and (len(current) == 5 or load + estimate > ROWS_PER_BATCH_TARGET):
            batches.append(current)
            current, load = [], 0
        current.append(advertiser)
        load += estimate
    if current:
        batches.append(current)
    return batches


def _timezones(session, tenant_id, advertiser_ids) -> dict[str, str | None]:
    cached = session.info.get("reporting_phase_timezones")
    if (
        cached is not None
        and cached[0] is session.get_transaction()
        and cached[1] == tenant_id
        and all(a in cached[2] for a in advertiser_ids)
    ):
        return {a: cached[2][a] for a in advertiser_ids}
    return dict(
        session.exec(
            select(AdvertiserAccount.advertiser_id, AdvertiserAccount.timezone).where(
                AdvertiserAccount.tenant_id == tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(advertiser_ids),
            )
        ).all()
    )


def _hot_window(timezone: str, now: datetime) -> tuple[date, date]:
    today = local_today(timezone, now)
    # 当前日独占热报容量；昨天由独立收尾入口补一次，不能每轮搬运旧窗口。
    return today, today


def _common(plan: AdSyncBc) -> dict:
    return {
        "tenant_id": plan.tenant_id,
        "bc_id": plan.bc_id,
        "frozen_route": plan.frozen_route,
        "actor_id": plan.actor_id,
    }


def _group_by_window(
    session, plan, advertisers, window_fn
) -> dict[tuple[date, date], list[str]]:
    grouped: dict[tuple[date, date], list[str]] = {}
    for advertiser, timezone in _timezones(
        session, plan.tenant_id, advertisers
    ).items():
        grouped.setdefault(window_fn(timezone), []).append(advertiser)
    return grouped


# 筛选时平台确认窗口内零消耗的账户不会再建报表任务；为它们记一条“已完成、
# 0 行”的同步状态，否则这些账户永远显示缺数据，汇总也会因一个缺失成员整体为空。
CONFIRMED_EMPTY_KINDS = ("ad_day", "account_day", "material_day")


def mark_confirmed_empty(
    session,
    plan: AdSyncBc,
    *,
    inactive: Sequence[str],
    window_fn,
    now: datetime,
    observed_at: datetime | None = None,
) -> int:
    """平台筛查确认这些账户在窗口内无消耗：补齐从未成功或失败的日期为完成 0 行。

    有事实的日期保留原有行数与成功时间；筛查不能代替完整报表读取。
    完整确认空桶默认采用 PG 完成时刻；跨轮规划传入 observed_at 时保留筛查证据时点。
    """
    del now  # 计划时钟不作为发布水位；窗口日期已由 window_fn 决定。
    if not inactive:
        return 0
    windows = _group_by_window(session, plan, inactive, window_fn)
    scopes = sorted(
        f"report:{plan.tenant_id}:{account}:{kind}"
        for account in {a for accounts in windows.values() for a in accounts}
        for kind in ("ad_day", "material_day")
    )
    if not scopes:
        return 0
    # 锁与发布器完全相同，仍按固定顺序领取；一次 SQL 取完，避免数百次网络往返。
    session.connection().execute(
        text("""
        WITH ordered AS MATERIALIZED (
            SELECT scope FROM unnest(CAST(:scopes AS text[])) AS locks(scope)
            ORDER BY scope)
        SELECT pg_advisory_xact_lock(hashtextextended(scope, 0)) FROM ordered
        """),
        {"scopes": scopes},
    ).all()
    completed_at = (
        session.connection().execute(text("SELECT clock_timestamp()")).scalar_one()
    )
    # 跨轮规划的零桶证据只能沿用真实筛查时点，不能把排队时间变成新成功。
    if observed_at is not None:
        completed_at = min(completed_at, observed_at)
    keys = """
        SELECT a.advertiser_id, k.report_kind, CAST(:start AS date) + d.day_offset AS stat_date
        FROM unnest(CAST(:accounts AS text[])) AS a(advertiser_id)
        CROSS JOIN unnest(CAST(:kinds AS text[])) AS k(report_kind)
        CROSS JOIN generate_series(0, :days - 1) AS d(day_offset)
    """
    marked = 0
    for (start, end), advertisers in windows.items():
        params = {
            "tenant": plan.tenant_id,
            "accounts": advertisers,
            "kinds": list(CONFIRMED_EMPTY_KINDS),
            "start": start,
            "days": (end - start).days + 1,
            "completed": completed_at,
        }
        # 已持有全部发布锁，先统计真正新确认的空桶；旧 COMPLETE/0 只刷新水位。
        marked += (
            session.connection()
            .execute(
                text(f"""
            SELECT count(*) FROM ({keys}) candidates
            LEFT JOIN report_sync_state s ON s.tenant_id=:tenant
                AND s.advertiser_id=candidates.advertiser_id
                AND s.report_kind=candidates.report_kind AND s.stat_date=candidates.stat_date
            WHERE s.status IS DISTINCT FROM 'COMPLETE'
              AND (s.last_success_at IS NULL OR s.last_success_at<=:completed)
            """),
                params,
            )
            .scalar_one()
        )
        # PG 批量生成日期和类型，一次写完整窗口；已有非空成功事实不改行数与时间。
        session.connection().execute(
            text(f"""
            INSERT INTO report_sync_state AS existing
                (tenant_id,advertiser_id,report_kind,stat_date,status,row_count,last_success_at,updated_at)
            SELECT :tenant,advertiser_id,report_kind,stat_date,'COMPLETE',0,:completed,:completed
            FROM ({keys}) candidates WHERE true
            ON CONFLICT (tenant_id,advertiser_id,report_kind,stat_date) DO UPDATE SET
                status='COMPLETE',row_count=0,
                error_code=CASE WHEN existing.status='COMPLETE' THEN existing.error_code ELSE NULL END,
                last_success_at=EXCLUDED.last_success_at,updated_at=EXCLUDED.updated_at
            WHERE (existing.status<>'COMPLETE' OR existing.row_count=0)
              AND (existing.last_success_at IS NULL OR existing.last_success_at<=EXCLUDED.last_success_at)
            """),
            params,
        )
    # 原函数会刷新已缓存的状态；批量 SQL 后过期同类 ORM 缓存，避免后续读到旧零值。
    for state in list(session.identity_map.values()):
        if isinstance(state, ReportSyncState):
            session.expire(state)
    return marked


def known_reporting_accounts(session, plan, *, accounts, now) -> set[str]:
    """当前当地日已有广告/素材事实的账户继续热刷，零消耗不能当作空报表。"""
    days: dict[date, list[str]] = {}
    for advertiser, timezone in _timezones(session, plan.tenant_id, accounts).items():
        try:
            day = local_today(timezone, now)
        except ZoneInfoNotFoundError, ValueError, TypeError:
            # 未知时区不能借用 UTC 或事实旧时区扩大当前日范围。
            continue
        days.setdefault(day, []).append(advertiser)
    known: set[str] = set()
    for day, candidates in days.items():
        # LATERAL LIMIT 阻止 EXISTS OR 被改成整日事实哈希扫描；广告命中后
        # 不再读取素材。候选已按账户自身时区分日，仍保留零消耗事实。
        known.update(
            session.connection()
            .execute(
                text("""
            SELECT a.advertiser_id
            FROM unnest(CAST(:accounts AS text[])) a(advertiser_id)
            LEFT JOIN LATERAL (
              SELECT true AS found FROM ad_report_day r
              WHERE r.tenant_id=:tenant AND r.advertiser_id=a.advertiser_id
                AND r.stat_date=:day LIMIT 1
            ) ad ON true
            LEFT JOIN LATERAL (
              SELECT true AS found FROM material_report_day r
              WHERE ad.found IS NULL AND r.tenant_id=:tenant
                AND r.advertiser_id=a.advertiser_id AND r.stat_date=:day LIMIT 1
            ) mat ON true
            WHERE ad.found OR mat.found
        """),
                {"tenant": plan.tenant_id, "accounts": candidates, "day": day},
            )
            .scalars()
        )
    return known


def plan_hot(
    session, plan: AdSyncBc, *, active: Sequence[str], now: datetime, tier: str = "hot"
) -> int:
    count = 0
    for (start, end), advertisers in _group_by_window(
        session, plan, active, lambda tz: _hot_window(tz, now)
    ).items():
        # 系列小时报仍按需读取；这里只批量写入原有广告日报任务。
        count += len(
            enqueue_jobs(
                session,
                kind="ad_day",
                tier=tier,
                advertiser_groups=batch_accounts(
                    session,
                    tenant_id=plan.tenant_id,
                    advertiser_ids=advertisers,
                    days=(end - start).days + 1,
                ),
                start=start,
                end=end,
                **_common(plan),
            )
        )
    return count


def plan_day_closeout(
    session: Session, plan: AdSyncBc, *, now: datetime, accounts=None
) -> int:
    """当地零点后为昨日有事实的账户收尾；成功水位与原计划防止重复规划。"""
    rows = (
        session.connection()
        .execute(
            text("""
        WITH owned AS (
          SELECT a.advertiser_id, a.timezone,
            (CAST(:now AS timestamptz) AT TIME ZONE a.timezone)::date AS today
          FROM advertiser_account a JOIN ad_sync_account o
            ON o.tenant_id=a.tenant_id AND o.advertiser_id=a.advertiser_id
          WHERE a.tenant_id=:tenant AND o.owner_bc_id=:bc
            AND (:all_accounts OR a.advertiser_id=ANY(:accounts))),
        yesterday AS (
          SELECT *, today-1 AS day, today::timestamp AT TIME ZONE timezone AS midnight
          FROM owned)
        SELECT y.advertiser_id,y.day,k.kind FROM yesterday y
        LEFT JOIN LATERAL (
          SELECT true AS found FROM ad_report_day f WHERE f.tenant_id=:tenant
            AND f.advertiser_id=y.advertiser_id AND f.stat_date=y.day LIMIT 1
        ) ad ON true
        LEFT JOIN LATERAL (
          SELECT true AS found FROM material_report_day f WHERE ad.found IS NULL
            AND f.tenant_id=:tenant AND f.advertiser_id=y.advertiser_id AND f.stat_date=y.day LIMIT 1
        ) mat ON true
        CROSS JOIN (VALUES ('ad_day'),('material_day')) k(kind)
        LEFT JOIN report_sync_state s ON s.tenant_id=:tenant
          AND s.advertiser_id=y.advertiser_id AND s.stat_date=y.day AND s.report_kind=k.kind
        WHERE (s.last_success_at IS NULL OR s.last_success_at<=y.midnight)
          AND (ad.found OR mat.found)
        ORDER BY y.day,k.kind,y.advertiser_id
        """),
            {
                "tenant": plan.tenant_id,
                "bc": plan.bc_id,
                "now": now,
                "all_accounts": accounts is None,
                "accounts": list(accounts or []),
            },
        )
        .all()
    )
    # 先证明昨天仍有缺口；已完成的BC不再每轮扫描全部收尾历史。
    if not rows:
        return 0
    # 收尾优先级固定为0，与历史范围部分索引一致；绑定参数会让generic计划
    # 无法证明索引谓词。保留全部状态和冻结路由过滤，终态旧任务也阻止重复规划。
    planned = set(
        session.connection()
        .execute(
            text("""
        SELECT ids.advertiser_id,j.end_date,j.kind FROM report_sync_job j
        CROSS JOIN LATERAL jsonb_array_elements_text(j.advertiser_ids) ids(advertiser_id)
        WHERE j.tenant_id=:tenant AND j.bc_id=:bc AND j.actor_id=:actor
          AND j.frozen_route=CAST(:route AS jsonb) AND j.priority=0
          AND j.start_date=j.end_date AND j.end_date BETWEEN :first_day AND :last_day
          AND j.advertiser_ids ?| CAST(:accounts AS text[])
          AND ids.advertiser_id=ANY(:accounts)
    """),
            {
                "tenant": plan.tenant_id,
                "bc": plan.bc_id,
                "actor": plan.actor_id,
                "route": json.dumps(plan.frozen_route),
                "first_day": min(row.day for row in rows),
                "last_day": max(row.day for row in rows),
                "accounts": sorted({row.advertiser_id for row in rows}),
            },
        )
        .all()
    )
    # 每账户/日期/类型只规划一次；失败交给原任务重试和每日回刷，不每轮重建。
    # planned 按本轮日期一次展开，避免对每个账户重复扫描完整任务历史。
    grouped: dict[tuple[str, date], list[str]] = {}
    for advertiser, day, kind in rows:
        if (advertiser, day, kind) in planned:
            continue
        grouped.setdefault((kind, day), []).append(advertiser)
    count = 0
    for (kind, day), advertisers in grouped.items():
        groups = (
            ([advertiser] for advertiser in advertisers)
            if kind == "material_day"
            else batch_accounts(
                session, tenant_id=plan.tenant_id, advertiser_ids=advertisers, days=1
            )
        )
        count += len(
            enqueue_jobs(
                session,
                kind=kind,
                tier="closeout",
                advertiser_groups=groups,
                start=day,
                end=day,
                due_at=now,
                **_common(plan),
            )
        )
    return count


def request_campaign_hours(
    session,
    *,
    tenant_id: UUID,
    bc_id: str,
    advertiser_ids: Sequence[str],
    start: date,
    end: date,
    now: datetime,
) -> bool:
    """小时趋势按需拉取：为缺失或已过期的账户本地日排入手动小时任务。

    返回是否仍在拉取中，页面据此轮询。今天、昨天的小时数据还在增长或被平台修正，
    超过热刷间隔视为过期；更早的日期完成后不再重拉。刚失败的日期在一个热刷间隔内
    不重排，避免页面轮询时对同一个失败请求反复重试。只为本 BC 负责的账户排队，
    其他 BC 负责的账户沿用其冻结路由，不能借当前 BC 的身份拉取。
    """
    plan = session.get(AdSyncBc, (tenant_id, bc_id))
    if not settings.ADS_SYNC_ENABLED or plan is None or not plan.enabled:
        return False
    owned = sorted(set(advertiser_ids) & set(owned_accounts(session, plan)))
    if not owned:
        return False
    states = {
        (s.advertiser_id, s.stat_date): s
        for s in session.exec(
            select(ReportSyncState).where(
                ReportSyncState.tenant_id == tenant_id,
                col(ReportSyncState.advertiser_id).in_(owned),
                ReportSyncState.report_kind == "campaign_hour",
                col(ReportSyncState.stat_date) >= start,
                col(ReportSyncState.stat_date) <= end,
            )
        ).all()
    }
    pending: dict[date, list[str]] = {}
    for advertiser, timezone in _timezones(session, tenant_id, owned).items():
        today = local_today(timezone or "UTC", now)
        day = start
        while day <= min(end, today):
            state = states.get((advertiser, day))
            if state is None:
                due = True
            elif state.status == "COMPLETE":
                due = (
                    day >= today - timedelta(days=1)
                    and state.last_success_at is not None
                    and state.last_success_at < now - HOUR_INTERVAL
                )
            else:
                due = state.updated_at < now - HOUR_INTERVAL
            if due:
                pending.setdefault(day, []).append(advertiser)
            day += timedelta(days=1)
    for day, advertisers in pending.items():
        for batch in batch_accounts(
            session, tenant_id=tenant_id, advertiser_ids=advertisers, days=1
        ):
            # 已在排队或执行中的相同任务按 job_key 去重，轮询不会重复入队。
            enqueue_job(
                session,
                kind="campaign_hour",
                tier="manual",
                advertiser_ids=batch,
                start=day,
                end=day,
                **_common(plan),
            )
    return bool(pending)


def plan_material(session, plan, *, active, now, tier: str = "material") -> int:
    count = 0
    for (start, end), advertisers in _group_by_window(
        session, plan, active, lambda tz: _hot_window(tz, now)
    ).items():
        # 素材读取仍每任务一个账户，数据库规划不再每账户查询整份在途覆盖。
        count += len(
            enqueue_jobs(
                session,
                kind="material_day",
                tier=tier,
                advertiser_groups=([advertiser] for advertiser in advertisers),
                start=start,
                end=end,
                **_common(plan),
            )
        )
    return count


def _shards(start: date, end: date, days: int = 30):
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def daily_window(now: datetime):
    """每日回刷窗口：各账户本地今天往前共 REFRESH_DAYS 天。"""

    def window(tz):
        return (
            local_today(tz, now) - timedelta(days=REFRESH_DAYS - 1),
            local_today(tz, now),
        )

    return window


def plan_daily(session, plan, *, active_32d, now) -> int:
    """每日修正已结束的日期；未执行的旧回刷覆盖重叠日期，不按天复制整月任务。"""
    if not active_32d:
        return 0
    pending = session.exec(
        select(
            col(ReportSyncJob.kind),
            col(ReportSyncJob.advertiser_ids),
            col(ReportSyncJob.start_date),
            col(ReportSyncJob.end_date),
        ).where(
            ReportSyncJob.tenant_id == plan.tenant_id,
            ReportSyncJob.bc_id == plan.bc_id,
            ReportSyncJob.actor_id == plan.actor_id,
            ReportSyncJob.frozen_route == plan.frozen_route,
            ReportSyncJob.priority >= 3,
            col(ReportSyncJob.status).in_(["QUEUED", "RUNNING", "WAITING"]),
            text("advertiser_ids ?| CAST(:daily_accounts AS text[])").bindparams(
                daily_accounts=list(active_32d)
            ),
        )
    ).all()
    covered = {}
    for kind, advertisers, first, last in pending:
        for advertiser in advertisers:
            covered.setdefault((kind, advertiser), []).append((first, last))
    count = 0
    for (start, current), advertisers in _group_by_window(
        session, plan, active_32d, daily_window(now)
    ).items():
        end = current - timedelta(days=1)  # 当前日由实时槽负责，冷档不重复拉它。
        for kind in ("ad_day", "account_day", "material_day"):
            windows = {}
            for advertiser in advertisers:
                first = None
                day = start
                while day <= end + timedelta(days=1):
                    due = day <= end and not any(
                        a <= day <= b for a, b in covered.get((kind, advertiser), [])
                    )
                    if due and first is None:
                        first = day
                    if not due and first is not None:
                        for a, b in _shards(first, day - timedelta(days=1)):
                            windows.setdefault((a, b), []).append(advertiser)
                        first = None
                    day += timedelta(days=1)
            for (a, b), ids in windows.items():
                batches = (
                    ([advertiser] for advertiser in ids)
                    if kind == "material_day"
                    else batch_accounts(
                        session,
                        tenant_id=plan.tenant_id,
                        advertiser_ids=ids,
                        days=(b - a).days + 1,
                    )
                )
                count += len(
                    enqueue_jobs(
                        session,
                        kind=kind,
                        tier="daily",
                        advertiser_groups=batches,
                        start=a,
                        end=b,
                        **_common(plan),
                    )
                )
    return count


def plan_history(session, plan, *, now, accounts=None, progress=None) -> int:
    """授权后一次：开户日至回刷窗口之前，按 30 天切片；已规划的账户不再重复。

    历史只回补广告日和素材日；系列小时报按需拉取，账户对账只在 32 天回刷内做。
    """
    count = 0
    rows = session.exec(
        select(AdSyncAccount, AdvertiserAccount)
        .join(
            AdvertiserAccount,
            (col(AdvertiserAccount.tenant_id) == AdSyncAccount.tenant_id)
            & (col(AdvertiserAccount.advertiser_id) == AdSyncAccount.advertiser_id),
        )
        .where(
            AdSyncAccount.tenant_id == plan.tenant_id,
            AdSyncAccount.owner_bc_id == plan.bc_id,
            col(AdSyncAccount.history_planned_at).is_(None),
            *(
                [col(AdSyncAccount.advertiser_id).in_(accounts)]
                if accounts is not None
                else []
            ),
        )
    ).all()
    for owner, account in rows:
        if account.remote_created_at is None or not account.timezone:
            continue  # 开户时间未知：等账户目录补齐后再规划
        end = local_today(account.timezone, now) - timedelta(days=REFRESH_DAYS)
        start = account.remote_created_at.astimezone(ZoneInfo(account.timezone)).date()
        if progress is not None and account.advertiser_id in progress:
            start = max(start, date.fromisoformat(progress[account.advertiser_id]))
        completed = True
        if start <= end:
            # 单账户多年历史也按原30日窗口提交续点，不能仅按账户缩批仍反复回滚。
            stop = min(end, start + timedelta(days=29)) if progress is not None else end
            # 规划只保存读取任务；历史月分区由发布端按目标事实表准备，
            # 不能在control规划事务持有两张事实父表的DDL锁。
            for s, e in _shards(start, stop):
                count += bool(
                    enqueue_job(
                        session,
                        kind="ad_day",
                        tier="history",
                        advertiser_ids=[account.advertiser_id],
                        start=s,
                        end=e,
                        **_common(plan),
                    )
                )
                count += bool(
                    enqueue_job(
                        session,
                        kind="material_day",
                        tier="history",
                        advertiser_ids=[account.advertiser_id],
                        start=s,
                        end=e,
                        **_common(plan),
                    )
                )
            if progress is not None and stop < end:
                progress[account.advertiser_id] = (stop + timedelta(days=1)).isoformat()
                completed = False
        if completed:
            owner.history_planned_at = now
            if progress is not None:
                progress.pop(account.advertiser_id, None)
    session.flush()
    return count


def estimate_round_requests(
    *, bcs: int, active: int, batch: int, hour_pages: float, ad_pages: float
) -> int:
    """一轮热刷请求量估算（素材按小时一次，折半计入每 30 分钟一轮）。"""
    batches = ceil(active / batch)
    return int(bcs + batches * ad_pages + batches * hour_pages + active / 2)


def _calls_key(now: datetime) -> str:
    return f"tiktok:report:calls:{now:%Y%m%d}"


def record_calls(redis, now: datetime, count: int) -> None:
    key = _calls_key(now)
    redis.incrby(key, count)
    redis.expire(key, 2 * 86400)


def calls_today(redis, now: datetime) -> int:
    return int(redis.get(_calls_key(now)) or 0)


def backfill_allowed(redis, now: datetime) -> bool:
    """只有运维显式设置的本地成本预算会暂停历史；不能冒充平台接口日额度。"""
    budget = settings.TIKTOK_REPORT_DAILY_CALL_BUDGET
    return budget == 0 or calls_today(redis, now) <= budget * 0.7
