from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.reporting import planner
from app.modules.reporting.health import observe_health
from app.modules.reporting.store_models import (
    AdSyncBc,
    ReportSyncHealthEvent,
    ReportSyncJob,
    ReportSyncState,
)
from tests.query_plans import explain_prepared


def test_missing_current_data_is_degraded_and_repaired_without_fake_success(
    session, sync_seed
):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    p = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    p.last_active_advertiser_ids = ["acc-2"]
    p.active_screened_at = now
    health = observe_health(session, p, now=now)
    assert health.status == "DEGRADED"
    assert (
        health.metrics["stale_ad_accounts"]
        == health.metrics["stale_material_accounts"]
        == 1
    )
    assert health.metrics["missing_directory_ads"] == 0
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {j.kind for j in jobs} == {"ad_day", "material_day"}
    assert all(j.advertiser_ids == ["acc-2"] for j in jobs)
    assert (
        session.get(
            ReportSyncState, (sync_seed.tenant_id, "acc-2", "ad_day", now.date())
        )
        is None
    )
    observe_health(session, p, now=now + timedelta(seconds=60))
    assert len(session.exec(select(ReportSyncHealthEvent)).all()) == 1
    assert len(session.exec(select(ReportSyncJob)).all()) == 2
    for kind in ["ad_day", "material_day"]:
        session.add(
            ReportSyncState(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                report_kind=kind,
                stat_date=now.date(),
                status="COMPLETE",
                last_success_at=now + timedelta(minutes=1),
            )
        )
    session.flush()
    health = observe_health(session, p, now=now + timedelta(minutes=2))
    assert health.status == "HEALTHY" and not health.issues
    assert len(session.exec(select(ReportSyncHealthEvent)).all()) == 2


def test_no_spend_scope_does_not_raise_false_staleness(session, sync_seed):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    p = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    p.last_active_advertiser_ids = ["acc-1"]  # 不属于该 BC 的规划所有权，不得越界修复。
    health = observe_health(session, p, now=now)
    assert health.status == "HEALTHY"
    assert session.exec(select(ReportSyncJob)).all() == []


def test_unknown_empty_active_scope_is_not_healthy(session, sync_seed):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids = []
    plan.active_screened_at = None
    health = observe_health(session, plan, now=now)
    assert health.status == "DEGRADED"
    assert "ACCOUNT_SCREEN_STALE" in health.issues
    jobs = session.exec(select(ReportSyncJob)).all()
    assert {adv for job in jobs for adv in job.advertiser_ids} == {"acc-1", "acc-2"}
    assert all(job.kind == "ad_day" for job in jobs)


def test_zero_spend_facts_are_monitored_before_active_screen_catches_up(
    session, sync_seed
):
    from app.modules.reporting.health import measure_health
    from tests.modules.reporting.test_planner import NOW, _zero_spend_fact

    now = NOW.replace(hour=20)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids = []
    plan.active_screened_at = now
    _zero_spend_fact(
        session, sync_seed.tenant_id, "material_day", now.date() + timedelta(days=1)
    )
    metrics, issues, repairs = measure_health(session, plan, now=now)
    assert metrics["stale_ad_accounts"] == metrics["stale_material_accounts"] == 1
    assert "stale_ad_accounts" in issues and "stale_material_accounts" in issues
    assert repairs["ad"] == repairs["material"] == {"acc-1"}


def test_health_observation_survives_contended_repair_lock(committed_sync_seed):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text

    from app.modules.reporting.store_models import ReportSyncHealth
    from app.modules.sync_control.models import SyncScope
    from app.modules.sync_control.scopes import ensure_scope

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        planner.ensure_bc_plans(session, now=now, tenant_id=seed.tenant_id)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids, plan.active_screened_at = ["acc-2"], now
        route = plan.frozen_route
        session.commit()
    with factory() as session:
        scope = ensure_scope(
            session,
            route=route,
            actor_id=seed.actor_id,
            advertiser_id="acc-2",
            resource="ad_day",
            query={"campaign_ids": []},
            timezone="UTC",
        )
        scope_id = scope.id
        session.commit()
    with factory() as holder:
        holder.exec(
            select(SyncScope).where(SyncScope.id == scope_id).with_for_update()
        ).one()

        def observe():
            with factory() as session:
                session.connection().execute(
                    text("SET LOCAL statement_timeout='500ms'")
                )
                plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
                observe_health(session, plan, now=now)
                session.commit()

        with ThreadPoolExecutor(1) as worker:
            worker.submit(observe).result(timeout=3)
        with factory() as session:
            health = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert health is not None and health.observed_at == now
            assert health.metrics["stale_ad_accounts"] == 1
            assert health.last_repair_at is None
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        observe_health(session, plan, now=now + timedelta(seconds=30))
        session.commit()
        assert session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()


@pytest.mark.parametrize("cached", [False, True])
def test_health_limits_history_scan_without_losing_local_date_boundaries(
    session, sync_seed, cached
):
    from uuid import uuid4

    from sqlalchemy import event, insert, text

    from app.modules.reporting.health import measure_health
    from app.modules.reporting.store_models import AdReportDay, MaterialReportDay

    # UTC 次日（+14）和前日（-12）都必须保留；同月历史不应参与当天覆盖查询。
    for hour, timezone, offset in [
        (20, "Pacific/Kiritimati", 1),
        (2, "Etc/GMT+12", -1),
    ]:
        now = datetime(2026, 10, 12, hour, tzinfo=UTC)
        planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
        plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids, plan.active_screened_at = ["acc-1"], now
        common = {
            "tenant_id": sync_seed.tenant_id,
            "advertiser_id": "acc-1",
            "currency": "USD",
            "timezone": timezone,
            "fetched_at": now,
            "sync_job_id": uuid4(),
            "spend": 1,
        }
        for model in (AdReportDay, MaterialReportDay):
            rows = []
            for index in range(5001):
                row = common | {
                    "stat_date": now.date() + timedelta(days=offset)
                    if index == 0
                    else now.date().replace(day=1)
                }
                identity = f"{hour}-{index}"
                row |= (
                    {"ad_id": identity, "campaign_id": "c", "adgroup_id": "g"}
                    if model is AdReportDay
                    else {"main_material_id": identity, "main_material_type": "VIDEO"}
                )
                rows.append(row)
            session.execute(insert(model), rows)
        session.flush()
        session.connection().execute(text("ANALYZE ad_report_day"))
        session.connection().execute(text("ANALYZE material_report_day"))
        queries = []

        def capture(_conn, _cursor, sql, params, _context, _many, queries=queries):
            if ("FROM ad_report_day r" in sql and "FROM ad_object" in sql) or (
                "SELECT DISTINCT r.advertiser_id,r.main_material_id" in sql
            ):
                queries.append((sql, params))

        connection = session.connection()
        if cached:
            connection.execute(text("SET LOCAL plan_cache_mode='force_generic_plan'"))
        event.listen(connection, "before_cursor_execute", capture)
        try:
            metrics, _, _ = measure_health(session, plan, now=now)
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        assert (
            metrics["missing_directory_ads"] == metrics["unlinked_material_posts"] == 1
        )
        assert len(queries) == 2

        def scanned(node):
            value = (
                node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
                if node.get("Relation Name", "").startswith(
                    ("ad_report_day", "material_report_day")
                )
                else 0
            )
            return value + sum(scanned(child) for child in node.get("Plans", []))

        for sql, params in queries:
            result = (
                explain_prepared(connection, sql, params)
                if cached
                else connection.exec_driver_sql(
                    "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params
                ).scalar_one()
            )
            assert scanned(result[0]["Plan"]) < 100, result
            if cached:

                def fact_conditions(node):
                    if node.get("Relation Name", "").startswith(
                        ("ad_report_day", "material_report_day")
                    ):
                        yield node.get("Index Cond", "")
                    for child in node.get("Plans", []):
                        yield from fact_conditions(child)

                # 宽BC的数组长度和日期不能在长连接计划里退化成未知参数；
                # 小夹具即使暂时扫描少，也必须保留实际参数参与索引代价判断。
                assert all(
                    "$" not in condition
                    for condition in fact_conditions(result[0]["Plan"])
                ), result
        # 下一组使用同一租户时去掉上组事实，保持独立当地日期证据。
        for model in (AdReportDay, MaterialReportDay):
            session.execute(
                model.__table__.delete().where(model.tenant_id == sync_seed.tenant_id)
            )


@pytest.mark.parametrize(
    "age,expected", [(15, "HEALTHY"), (20, "HEALTHY"), (21, "DEGRADED")]
)
def test_health_allows_realtime_cycle_and_execution_margin(
    session, sync_seed, age, expected
):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids, plan.active_screened_at = ["acc-2"], now
    for kind in ("ad_day", "material_day"):
        session.add(
            ReportSyncState(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                report_kind=kind,
                stat_date=now.date(),
                status="COMPLETE",
                last_success_at=now - timedelta(minutes=age),
            )
        )
    session.flush()
    health = observe_health(session, plan, now=now)
    assert health.status == expected
    assert len(session.exec(select(ReportSyncJob)).all()) == (2 if age > 20 else 0)


@pytest.mark.parametrize(
    "kind,age,rule,missing",
    [
        ("ad", 35, False, 0),
        ("ad", 38, False, 1),
        ("adgroup", 31, False, 0),
        ("adgroup", 65, False, 0),
        ("adgroup", 68, False, 1),
        ("campaign", 65, False, 0),
        ("campaign", 68, False, 1),
        ("ad", 20, True, 0),
        ("ad", 23, True, 1),
        ("adgroup", 20, True, 0),
        ("adgroup", 23, True, 1),
        ("campaign", 20, True, 0),
        ("campaign", 23, True, 1),
    ],
)
def test_directory_health_allows_layer_cycle_and_execution_margin(
    session, sync_seed, kind, age, rule, missing
):
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.reporting.health import measure_health
    from tests.modules.reporting.test_directory_refresh import (
        directory_objects,
        prepared,
    )

    now = datetime.now(UTC)
    prepared(session, sync_seed)
    directory_objects(session, sync_seed, now, ages={(kind, 0): age})
    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="health-rule",
            enabled=rule,
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={"accounts": {"mode": "ALL"}},
        )
    )
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    session.flush()
    metrics, _, repairs = measure_health(session, plan, now=now)
    assert metrics["missing_directory_ads"] == missing
    if missing:
        assert "acc-2" in repairs["directory"]
        assert not repairs["ad"]


@pytest.mark.parametrize("elapsed", [0.5, 1, 4, 5, 6, 7, 7.5])
@pytest.mark.parametrize("rule", [False, True])
def test_normal_hot_cycles_wait_for_directory_completion_without_ad_repairs(
    session, sync_seed, monkeypatch, rule, elapsed
):
    from app.core.config import settings
    from app.modules.ads.models import AdObject
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.reporting.directory_refresh import refresh_report_directory
    from app.modules.reporting.health import measure_health
    from tests.modules.reporting.test_directory_refresh import (
        directory_objects,
        prepared,
        runs_for,
    )

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    start = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0)
    job = prepared(session, sync_seed, day=start.date())
    job.status = "COMPLETE"
    # 模拟每次发布后完成目录，覆盖半周期边界并连续验证多轮不漂移。
    directory_objects(session, sync_seed, start + timedelta(minutes=elapsed))
    objects = {
        obj.kind: obj
        for obj in session.exec(
            select(AdObject).where(AdObject.tenant_id == sync_seed.tenant_id)
        ).all()
    }
    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="normal-cycle-rule",
            enabled=rule,
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={"accounts": {"mode": "ALL"}},
        )
    )
    planner.ensure_bc_plans(session, now=start, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids, plan.active_screened_at = ["acc-2"], start
    states = []
    for kind in ("ad_day", "material_day"):
        state = session.get(
            ReportSyncState, (sync_seed.tenant_id, "acc-2", kind, start.date())
        )
        if state is None:
            state = ReportSyncState(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                report_kind=kind,
                stat_date=start.date(),
            )
            session.add(state)
        state.status, state.last_success_at = "COMPLETE", start
        states.append(state)
    session.flush()
    planned = []
    moments = sorted(
        {
            base + offset
            for base in range(15, 181, 15)
            for offset in (0, elapsed - 0.01, elapsed)
        }
    )
    for minute in moments:
        now = start + timedelta(minutes=minute)
        if minute % 15 == 0:
            # 模拟正常热报发布：报表保持新鲜，只有目录延迟可能触发多余补拉。
            for state in states:
                state.last_success_at = now
            plan.active_screened_at = now
            session.flush()
            planned.append(refresh_report_directory(session, job, now=now))
        if abs(minute % 15 - elapsed) < 0.000001:
            for run in runs_for(session, sync_seed):
                if run.status == "QUEUED":
                    run.status = "COMPLETE"
                    objects[run.kind].observed_at = now
        session.flush()
        metrics, issues, repairs = measure_health(session, plan, now=now)
        assert metrics["missing_directory_ads"] == 0, (rule, minute, metrics)
        assert not issues and not repairs["ad"], (rule, minute, repairs)
        health = observe_health(session, plan, now=now)
        assert health.status == "HEALTHY"
        assert health.last_repair_at is None
        assert session.exec(select(ReportSyncJob)).all() == [job]
    # 规则账户每轮三层；普通账户广告每30分钟、上级每60分钟，规划频率不变。
    assert planned == ([3] * 12 if rule else [0, 1, 0, 3] * 3)


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Shanghai", "America/Los_Angeles"])
@pytest.mark.parametrize(
    "minutes,age,expected_stale",
    [(0, 5, 0), (10, 20, 0), (10, 20.01, 1), (19.99, 5, 0), (20, 5, 1), (21, 5, 1)],
)
def test_midnight_health_uses_same_freshness_as_rules(
    session, sync_seed, timezone, minutes, age, expected_stale
):
    from zoneinfo import ZoneInfo

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.automation_rules.inputs import AccountInfo, _fresh_accounts
    from app.modules.reporting.health import measure_health

    local_midnight = datetime(2026, 10, 21, tzinfo=ZoneInfo(timezone))
    now = local_midnight.astimezone(UTC) + timedelta(minutes=minutes)
    account = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-2"))
    account.timezone = timezone
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids, plan.active_screened_at = ["acc-2"], now
    for kind in ("ad_day", "material_day"):
        session.add(
            ReportSyncState(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                report_kind=kind,
                stat_date=local_midnight.date() - timedelta(days=1),
                status="COMPLETE",
                last_success_at=now - timedelta(minutes=age),
            )
        )
    session.flush()
    metrics, _, repairs = measure_health(session, plan, now=now)
    assert metrics["stale_ad_accounts"] == expected_stale
    assert metrics["stale_material_accounts"] == expected_stale
    for kind in ("ad_day", "material_day"):
        fresh, _ = _fresh_accounts(
            session,
            tenant_id=sync_seed.tenant_id,
            accounts={"acc-2": AccountInfo("acc-2", "bc-a", timezone, "USD", "A")},
            kind=kind,
            now=now,
        )
        assert bool(fresh) is (not expected_stale)
    assert repairs["ad"] == ({"acc-2"} if expected_stale else set())
