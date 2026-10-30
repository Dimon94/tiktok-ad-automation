"""缺目录直接定向补齐，不能靠重复读取已新鲜的日报驱动。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import health, planner
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob
from tests.modules.reporting.test_directory_refresh import prepared


def _case(session, seed, *, count=1):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=seed.tenant_id)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    plan.active_screened_at = now
    plan.last_active_advertiser_ids = ["acc-2"]
    job = prepared(
        session,
        seed,
        day=now.date(),
        count=count,
        types=["UPGRADED_SMART_PLUS"] * count,
    )
    job.status = "COMPLETE"
    session.flush()
    return now, plan, job


@pytest.mark.parametrize(
    "pending_status", ["QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE"]
)
def test_fresh_fact_coverage_repair_only_creates_targeted_directory(
    session, sync_seed, monkeypatch, pending_status
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, original = _case(session, sync_seed)
    measurement = health.measure_coverage(session, plan, now=now)
    assert measurement[0]["missing_directory_ads"] == 1
    health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    session.flush()
    jobs = session.exec(select(ReportSyncJob)).all()
    assert [job.id for job in jobs] == [original.id]
    runs = session.exec(select(AdDirectoryRun)).all()
    assert {(run.kind, tuple(run.query["ids"])) for run in runs} == {
        ("campaign", ("c0",)),
        ("adgroup", ("g0",)),
        ("ad", ("a0",)),
    }
    assert all(
        run.workload == "live"
        and run.actor_id == plan.actor_id
        and run.frozen_route == plan.frozen_route
        for run in runs
    )
    for run in runs:
        run.status = pending_status
        session.add(run)
    session.flush()
    health.observe_health(
        session,
        plan,
        now=now + timedelta(seconds=1),
        measurement=measurement,
        phase="coverage",
    )
    assert {run.id for run in session.exec(select(AdDirectoryRun)).all()} == {
        run.id for run in runs
    }
    assert [job.id for job in session.exec(select(ReportSyncJob)).all()] == [
        original.id
    ]


def test_dense_directory_repair_preserves_progress_inside_two_seconds(
    session, sync_seed, monkeypatch, record_property
):
    """50户各2000条今日广告及20万终态目录，真实2秒修复必须提交实际请求。"""
    import json
    from time import monotonic

    from sqlalchemy import text

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, original = _case(session, sync_seed)
    params = {
        "tenant": plan.tenant_id,
        "actor": plan.actor_id,
        "connection": sync_seed.connection.id,
        "route": json.dumps(plan.frozen_route),
        "now": now,
        "day": now.date(),
    }
    session.execute(
        text("""
      INSERT INTO advertiser_account(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
      SELECT :tenant,'dense-'||lpad(i::text,3,'0'),'','USD','UTC','ENABLE',false FROM generate_series(1,50)i
    """),
        params,
    )
    session.execute(
        text("""
      INSERT INTO bc_account_access(tenant_id,bc_id,advertiser_id,connection_id,in_bc,authorized,active,can_upload,can_build,permission_state,checked_at)
      SELECT :tenant,'bc-a','dense-'||lpad(i::text,3,'0'),:connection,true,true,true,false,false,'UNKNOWN',:now FROM generate_series(1,50)i
    """),
        params,
    )
    session.execute(
        text("""
      INSERT INTO ad_sync_account(tenant_id,advertiser_id,owner_bc_id)
      SELECT :tenant,'dense-'||lpad(i::text,3,'0'),'bc-a' FROM generate_series(1,50)i
    """),
        params,
    )
    session.execute(
        text("""
      INSERT INTO ad_report_day(tenant_id,advertiser_id,stat_date,ad_id,adgroup_id,campaign_id,
        campaign_automation_type,currency,timezone,fetched_at,sync_job_id,spend)
      SELECT :tenant,'dense-'||lpad(i::text,3,'0'),:day,'ad-'||n,'group','campaign',
        'UPGRADED_SMART_PLUS','USD','UTC',:now,gen_random_uuid(),1
      FROM generate_series(1,50)i CROSS JOIN generate_series(1,2000)n
    """),
        params,
    )
    session.execute(
        text("""
      INSERT INTO ad_directory_run(id,tenant_id,advertiser_id,bc_id,actor_id,connection_id,channel,
        frozen_route,request_id,partition_key,query,status,claim_generation,next_page,coverage,
        created_at,kind,ad_type,workload,publish_failures)
      SELECT gen_random_uuid(),:tenant,'dense-'||lpad((1+i%50)::text,3,'0'),'bc-a',:actor,:connection,
        'OFFICIAL_API',CAST(:route AS jsonb),gen_random_uuid(),lpad(i::text,64,'0'),'{}',
        'COMPLETE',1,1,'COMPLETE',:now,'ad','SMART_PLUS','live',0
      FROM generate_series(1,200000)i
    """),
        params,
    )
    session.execute(text("ANALYZE ad_report_day"))
    session.execute(text("ANALYZE ad_directory_run"))
    accounts = [f"dense-{i:03d}" for i in range(1, 51)]
    measurement = health.measure_coverage(session, plan, now=now, accounts=accounts)
    assert measurement[0]["missing_directory_ads"] == 100000
    started = monotonic()
    row = health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    session.flush()
    elapsed = monotonic() - started
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.status == "QUEUED")
    ).all()
    record_property("repair_elapsed", elapsed)
    record_property("created_runs", len(runs))
    assert row.metrics["missing_directory_ads"] == 100000
    assert [job.id for job in session.exec(select(ReportSyncJob)).all()] == [
        original.id
    ]
    assert runs, (
        "Moving directory planning into control must not roll back all progress at 2s"
    )
    assert elapsed < 2.5


def test_full_measurement_keeps_real_stale_fact_repair_separate(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.store_models import ReportSyncState

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, original = _case(session, sync_seed)
    for kind in ("ad_day", "material_day"):
        state = session.get(
            ReportSyncState, (plan.tenant_id, "acc-2", kind, now.date())
        )
        if state is None:
            state = ReportSyncState(
                tenant_id=plan.tenant_id,
                advertiser_id="acc-2",
                report_kind=kind,
                stat_date=now.date(),
                status="COMPLETE",
            )
        state.last_success_at = now - timedelta(minutes=21 if kind == "ad_day" else 1)
        session.add(state)
    session.flush()
    metrics, issues, repairs = health.measure_health(session, plan, now=now)
    assert repairs["ad"] == repairs["directory"] == {"acc-2"}
    assert repairs["material"] == set()
    health.observe_health(
        session, plan, now=now, measurement=(metrics, issues, repairs)
    )
    jobs = [j for j in session.exec(select(ReportSyncJob)).all() if j.id != original.id]
    assert len(jobs) == 1 and jobs[0].kind == "ad_day"
    assert len(session.exec(select(AdDirectoryRun)).all()) == 3


def test_old_coverage_redis_ad_set_continues_as_directory_only(
    committed_sync_seed, redis_client, monkeypatch
):
    import json

    from app.core.context import TenantContext

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        now, plan, original = _case(session, seed)
        original_id = original.id
        scope = health._health_scope(
            session, plan, health.owned_accounts(session, plan), now=now
        )
        progress = health._new_coverage_progress(scope)
        progress["repairs"] = {"ad": ["acc-2"], "material": [], "links": []}
        progress["metrics"] = {"missing_directory_ads": 1, "unlinked_material_posts": 0}
        progress["issues"] = ["missing_directory_ads"]
        progress["offset"] = 2
        for part in progress["stages"].values():
            part["offset"] = 2
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    redis_client.set(key, json.dumps(progress), ex=180)
    try:
        result = health.run_bc_health(
            database_engine=factory().bind,
            redis_client=redis_client,
            context=TenantContext(seed.tenant_id, seed.actor_id, "operator"),
            bc_id="bc-a",
            cycle=progress["cycle"],
        )
        assert result == "COMPLETE"
        assert redis_client.get(key) is None
        with factory() as session:
            assert [j.id for j in session.exec(select(ReportSyncJob)).all()] == [
                original_id
            ]
            assert len(session.exec(select(AdDirectoryRun)).all()) == 3
    finally:
        redis_client.delete(key)


def test_unknown_type_waits_for_normal_report_discovery_without_refresh_loop(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.store_models import AdReportDay

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, original = _case(session, sync_seed)
    fact = session.get(AdReportDay, (plan.tenant_id, "acc-2", now.date(), "a0"))
    fact.campaign_automation_type = None
    session.add(fact)
    session.flush()
    measurement = health.measure_coverage(session, plan, now=now)
    health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    assert [j.id for j in session.exec(select(ReportSyncJob)).all()] == [original.id]
    assert not session.exec(select(AdDirectoryRun)).all()
    known = planner.known_reporting_accounts(session, plan, accounts=["acc-2"], now=now)
    assert known == {"acc-2"}
    assert (
        planner.plan_hot(
            session, plan, active=sorted(known), now=now + timedelta(minutes=15)
        )
        == 1
    )
    # 下一普通轮获得类型事实后可直接补目录，不需要健康再造日报。
    fact.campaign_automation_type = "UPGRADED_SMART_PLUS"
    session.add(fact)
    session.flush()
    measurement = health.measure_coverage(session, plan, now=now)
    health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    assert len(session.exec(select(ReportSyncJob)).all()) == 2
    assert len(session.exec(select(AdDirectoryRun)).all()) == 3


def test_partial_id_chunk_commits_and_resumes_after_real_sql_cancel(
    committed_sync_seed, monkeypatch
):
    from sqlalchemy import event, text

    from app.modules.reporting.store_models import ReportSyncHealth

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        now, plan, original = _case(session, seed, count=205)
        original_id = original.id
        measurement = health.measure_coverage(session, plan, now=now)
    attempts = []
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        connection = session.connection()

        def slow_second(conn, _cursor, statement, _params, _context, _many):
            if statement.startswith("INSERT INTO ad_directory_run "):
                attempts.append(statement)
                if len(attempts) == 2:
                    conn.execute(text("SELECT pg_sleep(3)"))

        event.listen(connection, "before_cursor_execute", slow_second)
        try:
            health.observe_health(
                session, plan, now=now, measurement=measurement, phase="coverage"
            )
        finally:
            event.remove(connection, "before_cursor_execute", slow_second)
    with factory() as session:
        initial = session.exec(select(AdDirectoryRun)).all()
        assert len(initial) == 1 and len(initial[0].query["ids"]) == 100
        initial_id = initial[0].id
        state = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics[
            health._DIRECTORY_REPAIR_KEY
        ]
        assert state["after"] == "acc-2"
        assert state["unfinished"] == ["acc-2"]  # 访问已前移，但不伪报完成。
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        health.observe_health(
            session, plan, now=now, measurement=measurement, phase="coverage"
        )
    with factory() as session:
        runs = session.exec(select(AdDirectoryRun)).all()
        assert initial_id in {r.id for r in runs}
        ad_ids = [id for r in runs if r.kind == "ad" for id in r.query["ids"]]
        assert len(ad_ids) == len(set(ad_ids)) == 205
        assert {r.kind for r in runs} == {"ad", "adgroup", "campaign"}
        state = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics[
            health._DIRECTORY_REPAIR_KEY
        ]
        assert state["after"] == "acc-2"
        assert not state["unfinished"]
        assert [j.id for j in session.exec(select(ReportSyncJob)).all()] == [
            original_id
        ]


def test_later_regular_lane_cancel_cannot_erase_directory_and_gets_full_next_turn(
    committed_sync_seed, monkeypatch
):
    from uuid import uuid4

    from sqlalchemy import event, text

    from app.modules.reporting.store_models import MaterialReportDay, ReportSyncHealth

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        now, plan, original = _case(session, seed)
        original_id = original.id
        session.add(
            MaterialReportDay(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-2",
                stat_date=now.date(),
                main_material_id="missing-video",
                main_material_type="VIDEO",
                currency="USD",
                timezone="UTC",
                fetched_at=now,
                sync_job_id=uuid4(),
                spend=1,
            )
        )
        session.flush()
        measurement = health.measure_coverage(session, plan, now=now)
        assert measurement[2]["links"] == {"acc-2"}
    for attempt in (1, 2):
        slept = False
        with factory() as session, session.begin():
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            conn = session.connection()

            def delay(
                conn, _cursor, statement, _params, _context, _many, attempt=attempt
            ):
                nonlocal slept
                if not slept and statement.startswith("INSERT INTO report_sync_job "):
                    slept = True
                    conn.execute(
                        text(
                            "SELECT pg_sleep(3)"
                            if attempt == 1
                            else "SELECT pg_sleep(1.1)"
                        )
                    )

            event.listen(conn, "before_cursor_execute", delay)
            try:
                health.observe_health(
                    session, plan, now=now, measurement=measurement, phase="coverage"
                )
            finally:
                event.remove(conn, "before_cursor_execute", delay)
        with factory() as session:
            assert len(session.exec(select(AdDirectoryRun)).all()) == 3
            new = [
                j
                for j in session.exec(select(ReportSyncJob)).all()
                if j.id != original_id
            ]
            if attempt == 1:
                assert not new
                state = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics[
                    health._DIRECTORY_REPAIR_KEY
                ]
                assert state["after"] == "acc-2" and state["next"] == "regular"
            else:
                assert len(new) == 1 and new[0].kind == "material_links"


def _directory_accounts(session, seed, now, *, count, known):
    from uuid import uuid4

    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.reporting.store_models import AdReportDay, AdSyncAccount

    accounts = [f"fair-{i:03d}" for i in range(1, count + 1)]
    for account in accounts:
        session.add(
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id=account,
                timezone="UTC",
                currency="USD",
            )
        )
    session.flush()
    for account in accounts:
        session.add(
            BCAccountAccess(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                advertiser_id=account,
                connection_id=seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=now,
            )
        )
        session.add(
            AdSyncAccount(
                tenant_id=seed.tenant_id, advertiser_id=account, owner_bc_id="bc-a"
            )
        )
        session.add(
            AdReportDay(
                tenant_id=seed.tenant_id,
                advertiser_id=account,
                stat_date=now.date(),
                ad_id="ad",
                adgroup_id="group",
                campaign_id="campaign",
                currency="USD",
                timezone="UTC",
                spend=1,
                fetched_at=now,
                sync_job_id=uuid4(),
                campaign_automation_type="UPGRADED_SMART_PLUS"
                if known or account == accounts[-1]
                else None,
            )
        )
    session.flush()
    return accounts


@pytest.mark.parametrize("prefix", ["pending", "unknown"])
def test_first_fifty_non_actionable_accounts_do_not_hide_later_gap(
    session, sync_seed, monkeypatch, prefix
):
    from app.modules.reporting.directory_refresh import refresh_account_directory

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, original = _case(session, sync_seed)
    accounts = _directory_accounts(
        session, sync_seed, now, count=51, known=prefix == "pending"
    )
    if prefix == "pending":
        result = refresh_account_directory(
            session, **planner._common(plan), advertiser_ids=accounts[:50], now=now
        )
        assert result.created == 150 and result.complete
    measurement = health.measure_coverage(session, plan, now=now, accounts=accounts)
    row = health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    assert row.metrics[health._DIRECTORY_REPAIR_KEY]["after"] == accounts[49]
    assert not session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.advertiser_id == accounts[-1])
    ).all()
    health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.advertiser_id == accounts[-1])
    ).all()
    assert {r.kind for r in runs} == {"campaign", "adgroup", "ad"}
    assert [j.id for j in session.exec(select(ReportSyncJob)).all()] == [original.id]


def test_repair_scope_change_restarts_before_previous_prefix(
    session, sync_seed, monkeypatch
):
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.reporting.store_models import AdReportDay

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, original = _case(session, sync_seed)
    accounts = _directory_accounts(session, sync_seed, now, count=51, known=False)
    measurement = health.measure_coverage(session, plan, now=now, accounts=accounts)
    row = health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    previous_scope = row.metrics[health._DIRECTORY_REPAIR_KEY]["scope"]
    assert row.metrics[health._DIRECTORY_REPAIR_KEY]["after"] == accounts[49]
    fact = session.get(AdReportDay, (plan.tenant_id, accounts[0], now.date(), "ad"))
    fact.campaign_automation_type = "UPGRADED_SMART_PLUS"
    session.add(fact)
    session.add(
        AutomationRule(
            tenant_id=plan.tenant_id,
            name="repair-scope",
            created_by=plan.actor_id,
            updated_by=plan.actor_id,
            definition={
                "accounts": {"mode": "ACCOUNTS", "advertiser_ids": [accounts[0]]}
            },
        )
    )
    session.flush()
    health.observe_health(
        session, plan, now=now, measurement=measurement, phase="coverage"
    )
    assert row.metrics[health._DIRECTORY_REPAIR_KEY]["scope"] != previous_scope
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.advertiser_id == accounts[0])
    ).all()
    assert {r.kind for r in runs} == {"campaign", "adgroup", "ad"}
    assert [j.id for j in session.exec(select(ReportSyncJob)).all()] == [original.id]


def test_directory_success_cannot_delay_real_freshness_repair(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.store_models import ReportSyncState

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, original = _case(session, sync_seed)
    state = session.get(
        ReportSyncState, (plan.tenant_id, "acc-2", "ad_day", now.date())
    )
    state.last_success_at = now - timedelta(minutes=21)
    session.add(state)
    session.add(
        ReportSyncState(
            tenant_id=plan.tenant_id,
            advertiser_id="acc-2",
            report_kind="material_day",
            stat_date=now.date(),
            status="COMPLETE",
            last_success_at=now,
        )
    )
    session.flush()
    coverage = health.measure_coverage(session, plan, now=now)
    health.observe_health(
        session, plan, now=now, measurement=coverage, phase="coverage"
    )
    assert len(session.exec(select(AdDirectoryRun)).all()) == 3
    assert [j.id for j in session.exec(select(ReportSyncJob)).all()] == [original.id]
    fresh = health.measure_freshness(session, plan, now=now + timedelta(seconds=1))
    assert (
        fresh[0]["stale_ad_accounts"] == 1 and fresh[0]["stale_material_accounts"] == 0
    )
    health.observe_health(
        session,
        plan,
        now=now + timedelta(seconds=1),
        measurement=fresh,
        phase="freshness",
    )
    jobs = [j for j in session.exec(select(ReportSyncJob)).all() if j.id != original.id]
    assert len(jobs) == 1 and jobs[0].kind == "ad_day"


def test_health_and_report_publication_share_directory_lock(
    committed_sync_seed, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.modules.reporting.directory_refresh import refresh_report_directory

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        now, plan, original = _case(session, seed, count=101)
        original_id = original.id
        measurement = health.measure_coverage(session, plan, now=now)
    barrier = Barrier(2)

    def run(coverage):
        with factory() as session, session.begin():
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            job = session.get(ReportSyncJob, original_id)
            barrier.wait(timeout=5)
            if coverage:
                health.observe_health(
                    session, plan, now=now, measurement=measurement, phase="coverage"
                )
            else:
                refresh_report_directory(session, job, now=now)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, value) for value in (True, False)]
        for future in futures:
            future.result(timeout=10)
    with factory() as session:
        runs = session.exec(select(AdDirectoryRun)).all()
        for kind, prefix in [("ad", "a"), ("adgroup", "g"), ("campaign", "c")]:
            ids = [id for r in runs if r.kind == kind for id in r.query["ids"]]
            assert len(ids) == len(set(ids)) == 101
            assert set(ids) == {f"{prefix}{i}" for i in range(101)}
        assert [j.id for j in session.exec(select(ReportSyncJob)).all()] == [
            original_id
        ]


def test_large_partial_account_yields_to_small_account_within_two_turns(
    session, sync_seed, monkeypatch
):
    from sqlalchemy import text

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now, plan, _ = _case(session, sync_seed)
    accounts = _directory_accounts(session, sync_seed, now, count=2, known=True)
    session.execute(
        text("""
        INSERT INTO ad_report_day(tenant_id,advertiser_id,stat_date,ad_id,
          adgroup_id,campaign_id,campaign_automation_type,currency,timezone,
          fetched_at,sync_job_id,spend)
        SELECT :tenant,:account,:day,'large-'||n,'group','campaign',
          'UPGRADED_SMART_PLUS','USD','UTC',:now,gen_random_uuid(),1
        FROM generate_series(1,100000)n
        """),
        {
            "tenant": plan.tenant_id,
            "account": accounts[0],
            "day": now.date(),
            "now": now,
        },
    )
    session.execute(text("ANALYZE ad_report_day"))
    measurement = health.measure_coverage(session, plan, now=now, accounts=accounts)
    for _ in range(2):
        row = health.observe_health(
            session, plan, now=now, measurement=measurement, phase="coverage"
        )
    runs = session.exec(select(AdDirectoryRun)).all()
    small = [r for r in runs if r.advertiser_id == accounts[1]]
    assert {r.kind for r in small} == {"ad", "adgroup", "campaign"}
    large_ids = [
        item
        for r in runs
        if r.advertiser_id == accounts[0] and r.kind == "ad"
        for item in r.query["ids"]
    ]
    assert 0 < len(large_ids) == len(set(large_ids)) < 100001
    assert accounts[0] in row.metrics[health._DIRECTORY_REPAIR_KEY]["unfinished"]
