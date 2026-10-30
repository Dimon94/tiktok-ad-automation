from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.planner import (
    account_owners,
    batch_accounts,
    ensure_bc_plans,
    plan_daily,
    plan_history,
    plan_hot,
    screen_active,
)
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob

NOW = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)


def test_auth_creates_plans_without_manual_refresh(session, sync_seed):
    assert ensure_bc_plans(session, now=NOW) == 2
    plans = session.exec(
        select(AdSyncBc).where(AdSyncBc.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {p.bc_id for p in plans} == {"bc-a", "bc-b"} and all(
        p.next_hot_at <= NOW and p.next_daily_at <= NOW for p in plans
    )


def test_connection_level_discovery_creates_official_api_plans(session, sync_seed):
    """Official API discovery is connection-wide and stores no BC scope."""
    from app.modules.accounts.models import DiscoveryRun

    for run in session.exec(
        select(DiscoveryRun).where(
            DiscoveryRun.tenant_id == sync_seed.tenant_id,
            DiscoveryRun.connection_id == sync_seed.connection.id,
        )
    ).all():
        run.bc_id = None
    session.flush()

    assert ensure_bc_plans(session, now=NOW) == 2
    plans = session.exec(
        select(AdSyncBc).where(AdSyncBc.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {p.bc_id for p in plans} == {"bc-a", "bc-b"}
    assert all(p.actor_id == sync_seed.actor_id for p in plans)


def test_shared_account_is_planned_once(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    owners = account_owners(session, tenant_id=sync_seed.tenant_id)
    assert owners == {
        "acc-1": "bc-a",
        "acc-2": "bc-a",
    }  # 按 bc_id 字典序选主 BC，稳定不抖动


def test_mcp_only_bc_is_not_planned(session, sync_seed):
    from app.modules.accounts.connection_models import BCConnectionBinding

    binding = session.get(
        BCConnectionBinding, (sync_seed.tenant_id, "bc-b", sync_seed.connection.id)
    )
    from app.modules.accounts.models import TikTokConnection

    mcp = TikTokConnection(
        tenant_id=sync_seed.tenant_id, kind="OFFICIAL_MCP", status="ACTIVE"
    )
    session.add(mcp)
    session.flush()
    binding.connection_id = mcp.id
    binding.kind = "OFFICIAL_MCP"  # 实际外键要求连接与绑定的通道一致
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    assert plan is None or (
        plan.enabled is False and plan.disabled_reason == "api_binding_required"
    )


def test_screen_uses_bc_report_and_filters_candidates():
    class R:
        def bc_spend(self, *, bc_id, start, end, page):
            rows = (
                {
                    "dimensions": {"advertiser_id": "acc-1"},
                    "metrics": {"spend": "3.10"},
                },
                {
                    "dimensions": {"advertiser_id": "acc-9"},
                    "metrics": {"spend": "1.00"},
                },
                {
                    "dimensions": {"advertiser_id": "acc-2"},
                    "metrics": {"spend": "0.000"},
                },
            )
            return RawReportPage(rows, 1, 1, 3)

    assert screen_active(
        R(),
        bc_id="bc-a",
        start=date(2026, 10, 12),
        end=date(2026, 10, 12),
        candidates={"acc-1", "acc-2"},
    ) == {"acc-1"}


@pytest.mark.parametrize("now", [NOW, NOW.replace(hour=20)])
def test_hot_plan_uses_current_local_day_and_daily_excludes_it(
    session, sync_seed, monkeypatch, now
):
    from zoneinfo import ZoneInfo

    from app.modules.reporting.planner import plan_material
    from app.modules.sync_control import report_demand

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    # planner窗口与需求归并必须使用同一测试时刻，避免固定日期被当成历史合并。
    monkeypatch.setattr(report_demand, "datetime", Clock)

    # UTC 午前也只读当天；UTC 晚间香港已跨日，不能全账户直接用 UTC 日期。
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=["acc-1", "acc-2"], now=now)
    plan_material(session, plan, active=["acc-1", "acc-2"], now=now)
    plan_daily(session, plan, active_32d=["acc-1", "acc-2"], now=now)
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    today = {
        "acc-1": now.astimezone(ZoneInfo("Asia/Hong_Kong")).date(),
        "acc-2": now.date(),
    }
    live = [job for job in jobs if job.priority <= 2]
    cold = [job for job in jobs if job.priority >= 3]
    assert {job.kind for job in live} == {"ad_day", "material_day"}
    assert {job.kind for job in cold} == {"ad_day", "material_day", "account_day"}
    for job in live:
        assert all(
            job.start_date == job.end_date == today[advertiser]
            for advertiser in job.advertiser_ids
        )
    for job in cold:
        assert all(
            job.end_date < today[advertiser] for advertiser in job.advertiser_ids
        )
    assert not [job for job in jobs if job.kind == "campaign_hour"]


def test_inactive_account_keeps_published_rows_until_daily_refresh(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=[], now=NOW)
    assert (
        session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
        ).all()
        == []
    )
    plan_daily(session, plan, active_32d=["acc-1"], now=NOW.replace(hour=13))
    kinds = {
        j.kind
        for j in session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
        ).all()
    }
    assert kinds == {"ad_day", "material_day", "account_day"}


def test_history_shards_30_days_from_account_creation(session, sync_seed):
    from app.modules.accounts.models import AdvertiserAccount

    acc = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-1"))
    acc.remote_created_at = datetime(2026, 7, 1, tzinfo=UTC)
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_history(session, plan, now=NOW)
    ad = sorted(
        (j.start_date, j.end_date)
        for j in session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == sync_seed.tenant_id,
                ReportSyncJob.kind == "ad_day",
            )
        ).all()
    )
    assert ad[0][0] == date(2026, 7, 1) and all((e - s).days < 30 for s, e in ad)
    assert ad[-1][1] < date(2026, 10, 12) - timedelta(days=31)
    plan_history(session, plan, now=NOW)  # 第二次不重复规划
    assert len(
        session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.kind == "ad_day",
                ReportSyncJob.tenant_id == sync_seed.tenant_id,
            )
        ).all()
    ) == len(ad)


def test_batch_accounts_shrinks_for_large_accounts(session, sync_seed):
    assert batch_accounts(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1", "acc-2"],
        days=1,
    ) == [["acc-1", "acc-2"]]


def test_ten_thousand_accounts_one_round_call_budget():
    """规模估算：1 万账户、2000 活跃、50 个 BC 的一轮热刷请求数在规格范围内。"""
    from app.modules.reporting.planner import estimate_round_requests

    assert (
        estimate_round_requests(
            bcs=50, active=2000, batch=5, hour_pages=1, ad_pages=1.5
        )
        <= 2200
    )


def test_after_utc_noon_days_only_today(session, sync_seed):
    from app.modules.reporting.planner import plan_material

    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=["acc-1", "acc-2"], now=NOW.replace(hour=13))
    plan_material(session, plan, active=["acc-1", "acc-2"], now=NOW.replace(hour=13))
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    for kind in ("ad_day", "material_day"):
        assert {(j.start_date, j.end_date) for j in jobs if j.kind == kind} == {
            (date(2026, 10, 12), date(2026, 10, 12))
        }
    assert not [j for j in jobs if j.kind == "campaign_hour"]


def test_removed_binding_disables_existing_plan(session, sync_seed):
    from app.modules.accounts.connection_models import BCConnectionBinding

    ensure_bc_plans(session, now=NOW)
    binding = session.get(
        BCConnectionBinding, (sync_seed.tenant_id, "bc-a", sync_seed.connection.id)
    )
    session.delete(binding)
    session.flush()
    ensure_bc_plans(session, now=NOW)
    assert session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a")).enabled is False
    assert "acc-1" not in account_owners(session, tenant_id=sync_seed.tenant_id)


def test_confirmed_zero_spend_accounts_count_as_complete(session, sync_seed):
    """完整零筛查更新空桶的 PG 成功时间，已有事实的成功时间必须保留。"""
    from app.modules.reporting.planner import (
        CONFIRMED_EMPTY_KINDS,
        daily_window,
        mark_confirmed_empty,
    )
    from app.modules.reporting.store_models import ReportSyncState

    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    kept = ReportSyncState(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        report_kind="ad_day",
        stat_date=date(2026, 10, 12),
        status="COMPLETE",
        row_count=7,
        last_success_at=NOW - timedelta(hours=1),
    )
    session.add(kept)
    session.flush()
    before = session.connection().execute(text("SELECT clock_timestamp()")).scalar_one()
    mark_confirmed_empty(
        session, plan, inactive=["acc-1"], window_fn=daily_window(NOW), now=NOW
    )
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == sync_seed.tenant_id,
            ReportSyncState.advertiser_id == "acc-1",
        )
    ).all()
    assert {s.report_kind for s in states} == set(CONFIRMED_EMPTY_KINDS)
    assert len(states) == 32 * len(CONFIRMED_EMPTY_KINDS)
    assert all(s.status == "COMPLETE" for s in states)
    assert (
        session.get(
            ReportSyncState,
            (sync_seed.tenant_id, "acc-1", "ad_day", date(2026, 10, 12)),
        ).row_count
        == 7
    )
    after = session.connection().execute(text("SELECT clock_timestamp()")).scalar_one()
    assert kept.last_success_at == NOW - timedelta(hours=1)
    empty = [state for state in states if state.row_count == 0]
    assert len(empty) == len(states) - 1
    assert all(
        before <= state.last_success_at <= after
        and state.updated_at == state.last_success_at
        for state in empty
    )


def test_removed_member_discovery_does_not_become_sync_actor(session, sync_seed):
    """已移出租户的成员即使最近跑过账户发现，也不能成为后台同步身份。"""
    from uuid import uuid4

    from app.models import User
    from app.modules.accounts.models import DiscoveryRun
    from app.modules.tenants.models import TenantMembership

    former = User(username=f"former-{uuid4().hex[:10]}", hashed_password="x")
    session.add(former)
    session.flush()
    session.add(
        TenantMembership(
            tenant_id=sync_seed.tenant_id,
            user_id=former.id,
            role="operator",
            active=False,
        )
    )
    session.add(
        DiscoveryRun(
            tenant_id=sync_seed.tenant_id,
            actor_id=former.id,
            connection_id=sync_seed.connection.id,
            bc_id=None,
            status="COMPLETE",
            completed_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    )
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    assert plan.enabled is True
    assert plan.actor_id == sync_seed.actor_id


def test_zero_screen_waits_for_publish_and_does_not_reuse_cached_zero_state(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep
    from uuid import uuid4

    from app.modules.reporting.planner import mark_confirmed_empty
    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from app.modules.reporting.store_models import ReportSyncState
    from tests.modules.reporting.sync_conftest import ad_row

    seed, factory = committed_sync_seed
    day = NOW.date()
    common = {
        "tenant_id": seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        "start": day,
        "end": day,
    }
    with factory() as session:
        ensure_bc_plans(session, now=NOW)
        publish_ad_day(session, **common, rows=[])
        session.commit()
    cached = Event()

    def confirm_zero():
        with factory() as session:
            session.connection().execute(
                text("SET LOCAL application_name = 'test-zero-screen-watermark'")
            )
            state = session.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", day)
            )
            assert state.row_count == 0
            cached.set()
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            mark_confirmed_empty(
                session,
                plan,
                inactive=["acc-1"],
                window_fn=lambda _tz: (day, day),
                now=NOW,
            )
            session.commit()

    with factory() as writer, ThreadPoolExecutor(max_workers=1) as pool:
        publish_ad_day(writer, **common, rows=[ad_row("new", day, spend="20")])
        published = writer.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", day)
        ).last_success_at
        future = pool.submit(confirm_zero)
        blocked = False
        try:
            assert cached.wait(5)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with factory() as observer:
                    blocked = (
                        observer.connection()
                        .execute(
                            text("""
                        SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
                        WHERE a.application_name='test-zero-screen-watermark'
                        AND l.locktype='advisory' AND NOT l.granted)
                    """)
                        )
                        .scalar_one()
                    )
                if blocked:
                    break
                sleep(0.01)
        finally:
            writer.commit()
        assert blocked
        future.result(timeout=5)
    with factory() as session:
        state = session.get(ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", day))
        assert state.row_count == 1 and state.last_success_at == published


def _zero_spend_fact(session, tenant_id, kind, day):
    from decimal import Decimal
    from uuid import uuid4

    from app.modules.reporting.store_models import AdReportDay, MaterialReportDay

    common = {
        "tenant_id": tenant_id,
        "advertiser_id": "acc-1",
        "stat_date": day,
        "currency": "USD",
        "timezone": "Asia/Hong_Kong",
        "fetched_at": NOW - timedelta(hours=1),
        "sync_job_id": uuid4(),
        "spend": Decimal("0"),
        "impressions": Decimal("1"),
    }
    fact = (
        AdReportDay(**common, ad_id="zero-ad", campaign_id="c", adgroup_id="g")
        if kind == "ad_day"
        else MaterialReportDay(
            **common, main_material_id="zero-post", main_material_type="VIDEO"
        )
    )
    session.add(fact)
    session.flush()
    return fact


@pytest.mark.parametrize("kind", ["ad_day", "material_day"])
def test_zero_spend_actual_facts_stay_hot_without_fabricating_watermark(
    session, sync_seed, kind
):
    from app.modules.reporting import planner
    from app.modules.reporting.store_models import ReportSyncState

    now = NOW.replace(hour=20)  # 香港已是次日，UTC 账户仍在前一天。
    day = date(2026, 10, 13)
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    fact = _zero_spend_fact(session, sync_seed.tenant_id, kind, day)
    state = ReportSyncState(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        report_kind=kind,
        stat_date=day,
        status="COMPLETE",
        row_count=1,
        last_success_at=NOW - timedelta(hours=1),
    )
    session.add(state)
    session.flush()
    active = planner.known_reporting_accounts(
        session, plan, accounts=planner.owned_accounts(session, plan), now=now
    )
    assert active == {"acc-1"}
    for _ in range(2):
        planner.plan_hot(session, plan, active=sorted(active), now=now)
        planner.plan_material(session, plan, active=sorted(active), now=now)
    planner.mark_confirmed_empty(
        session,
        plan,
        inactive=sorted({"acc-1", "acc-2"} - active),
        window_fn=lambda tz: planner._hot_window(tz, now),
        now=now,
    )
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(jobs) == 2 and {job.kind for job in jobs} == {"ad_day", "material_day"}
    assert all(
        job.advertiser_ids == ["acc-1"]
        and job.start_date == job.end_date == day
        and job.frozen_route == plan.frozen_route
        and job.actor_id == plan.actor_id
        for job in jobs
    )
    assert state.row_count == 1 and state.last_success_at == NOW - timedelta(hours=1)
    assert fact.spend == 0 and fact.impressions == 1
    empty = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-2", "ad_day", now.date())
    )
    assert empty.status == "COMPLETE" and empty.row_count == 0


@pytest.mark.parametrize("kind", ["ad_day", "material_day"])
@pytest.mark.parametrize(
    "excluded", ["yesterday", "tomorrow", "tenant", "ownership", "unknown_timezone"]
)
def test_known_facts_do_not_expand_local_day_or_account_scope(
    session, sync_seed, kind, excluded
):
    from uuid import uuid4

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting import planner

    now = NOW.replace(hour=20)
    day = date(2026, 10, 13)
    if excluded == "yesterday":
        day -= timedelta(days=1)
    elif excluded == "tomorrow":
        day += timedelta(days=1)
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    _zero_spend_fact(
        session, uuid4() if excluded == "tenant" else sync_seed.tenant_id, kind, day
    )
    if excluded == "unknown_timezone":
        session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-1")).timezone = ""
        session.flush()
    accounts = ["acc-2"] if excluded == "ownership" else ["acc-1", "acc-2"]
    assert (
        planner.known_reporting_accounts(session, plan, accounts=accounts, now=now)
        == set()
    )


@pytest.mark.parametrize("account_count", [2, 287])
def test_confirmed_empty_window_uses_bounded_database_round_trips(
    session, sync_seed, account_count
):
    """32 天空桶确认不能按账户×日期×类型逐条往返数据库。"""
    from sqlalchemy import event

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.planner import daily_window, mark_confirmed_empty

    accounts = ["acc-1", "acc-2"] + [f"empty-{i}" for i in range(account_count - 2)]
    for account in accounts[2:]:
        session.add(
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id, advertiser_id=account, timezone="UTC"
            )
        )
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    statements = []

    def collect(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", collect)
    try:
        first = mark_confirmed_empty(
            session,
            plan,
            inactive=accounts,
            window_fn=daily_window(NOW),
            now=NOW,
        )
    finally:
        event.remove(connection, "before_cursor_execute", collect)
    assert first == account_count * 32 * 3
    assert len(statements) <= 10
    assert (
        mark_confirmed_empty(
            session,
            plan,
            inactive=accounts,
            window_fn=daily_window(NOW),
            now=NOW,
        )
        == 0
    )


@pytest.mark.parametrize("account_count", [24, 586])
def test_material_cycle_enqueues_accounts_with_bounded_database_round_trips(
    session, sync_seed, monkeypatch, account_count
):
    from sqlalchemy import event

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.planner import plan_material
    from app.modules.sync_control import report_demand

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(report_demand, "datetime", Clock)

    accounts = ["acc-1", "acc-2"] + [f"material-{i}" for i in range(account_count - 2)]
    session.add_all(
        AdvertiserAccount(
            tenant_id=sync_seed.tenant_id, advertiser_id=account, timezone="UTC"
        )
        for account in accounts[2:]
    )
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    statements = []

    def collect(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", collect)
    try:
        assert plan_material(session, plan, active=accounts, now=NOW) == account_count
        first_count = len(statements)
        statements.clear()
        assert plan_material(session, plan, active=accounts, now=NOW) == 0
        repeated_count = len(statements)
    finally:
        event.remove(connection, "before_cursor_execute", collect)
    assert first_count <= 10
    assert repeated_count <= 10
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(jobs) == account_count
    assert all(
        job.kind == "material_day" and len(job.advertiser_ids) == 1 for job in jobs
    )


@pytest.mark.parametrize(
    "scope,enabled,expected",
    [
        ({"mode": "ALL"}, True, True),
        ({"mode": "BC", "bc_ids": ["bc-a"]}, True, True),
        ({"mode": "BC", "bc_ids": ["bc-b"]}, True, False),
        ({"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]}, True, True),
        ({"mode": "ACCOUNTS", "advertiser_ids": ["other"]}, True, False),
        ({"mode": "ALL"}, False, False),
    ],
)
def test_rule_sync_phase_uses_enabled_owned_accounts(
    session, sync_seed, scope, enabled, expected
):
    from app.modules.automation_rules.models import AutomationRule

    now = NOW.replace(minute=4, second=23)
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.next_hot_at = plan.next_material_at = now + timedelta(minutes=15)
    original = plan.next_hot_at
    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="同步相位",
            enabled=enabled,
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={"accounts": scope},
        )
    )
    session.flush()
    ensure_bc_plans(session, now=now)
    assert (
        plan.next_hot_at
        == plan.next_material_at
        == (NOW.replace(minute=12) if expected else original)
    )
    other = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    assert other.next_hot_at == now  # 没有归属账户的 BC 不借用共享账户改相位。


def test_rule_sync_enable_disable_restores_bc_phase_without_drift(session, sync_seed):
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.reporting.job_tasks import _advance

    now = NOW.replace(minute=4, second=23, microsecond=123456)
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    original = now + timedelta(minutes=15)
    plan.next_hot_at = plan.next_material_at = original
    rule = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="启停相位",
        enabled=True,
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition={"accounts": {"mode": "ALL"}},
    )
    session.add(rule)
    session.flush()
    ensure_bc_plans(session, now=now)
    for minutes in (13, 29, 63, 117):
        delayed = NOW + timedelta(minutes=minutes, seconds=41)
        next_at = _advance(plan.next_hot_at, delayed, timedelta(minutes=15))
        plan.next_hot_at = plan.next_material_at = next_at
        ensure_bc_plans(session, now=delayed)
        assert plan.next_hot_at == next_at == plan.next_material_at
        assert next_at.minute % 15 == 12 and next_at.second == next_at.microsecond == 0
    rule.enabled = False
    session.flush()
    ensure_bc_plans(session, now=delayed)
    assert (
        plan.next_hot_at
        == plan.next_material_at
        == _advance(original, delayed, timedelta(minutes=15))
    )
    assert plan.screening_progress == {}
    rule.enabled = True
    session.flush()
    ensure_bc_plans(session, now=delayed)
    assert plan.next_hot_at.minute % 15 == 12


def test_new_rule_bc_remains_due_and_anchors_before_grid(session, sync_seed):
    from app.modules.automation_rules.models import AutomationRule

    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="首轮不延误",
            enabled=True,
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={"accounts": {"mode": "BC", "bc_ids": ["bc-a"]}},
        )
    )
    session.flush()
    now = NOW.replace(minute=14)
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    assert plan.next_hot_at == plan.next_material_at == NOW.replace(minute=12)


@pytest.mark.parametrize("minute,expected", [(12, 12), (13, 27)])
def test_rule_sync_grid_boundary_and_failed_plan_backoff(
    session, sync_seed, minute, expected
):
    from app.modules.automation_rules.models import AutomationRule

    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.next_hot_at = plan.next_material_at = NOW + timedelta(minutes=20)
    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="刻度边界",
            enabled=True,
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={"accounts": {"mode": "ALL"}},
        )
    )
    session.flush()
    ensure_bc_plans(session, now=NOW.replace(minute=minute))
    assert plan.next_hot_at == plan.next_material_at == NOW.replace(minute=expected)
    plan.next_hot_at = NOW.replace(minute=33, second=7)
    ensure_bc_plans(session, now=NOW.replace(minute=30))
    assert plan.next_hot_at == plan.next_material_at == NOW.replace(minute=42)
