from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.reporting import job_tasks


def _set_background_window(request, capacity):
    """测试也配置真实共享池；退出时恢复部署参数和原调度游标。"""
    from copy import deepcopy

    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.sync_control.models import SyncWorkerPool

    with Session(engine) as session, session.begin():
        pool = session.get(SyncWorkerPool, "background")
        previous = deepcopy(pool.model_dump()) if pool is not None else None
        if pool is None:
            pool = SyncWorkerPool(pool_name="background")
            session.add(pool)
        pool.capacity, pool.prefetch_slots = capacity, 0
        pool.fairness_state, pool.readiness_cursor = {}, {}

    def restore():
        with Session(engine) as session, session.begin():
            pool = session.get(SyncWorkerPool, "background")
            if previous is None:
                if pool is not None:
                    session.delete(pool)
            else:
                for key, value in previous.items():
                    setattr(pool, key, value)

    request.addfinalizer(restore)


def _use_dispatch_redis(monkeypatch, redis_client):
    # 调用前redis_client fixture已验证独立TEST Redis，随后才将正式派发器指向它。
    import os

    assert redis_client.ping()
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])


def test_disabled_flag_plans_nothing(monkeypatch, committed_sync_seed, redis_client):
    _, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", False)
    from app.core.db import engine

    assert (
        job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client) == 0
    )


def test_plan_due_screens_with_bc_report_and_dispatches(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    calls = []

    class Reader:
        def bc_spend(self, *, bc_id, start, end, page):
            from app.integrations.tiktok.contracts.report_reader import RawReportPage

            calls.append(bc_id)
            rows = (
                {
                    "dimensions": {"advertiser_id": "acc-1"},
                    "metrics": {"spend": "1.00"},
                },
            )
            return RawReportPage(rows, 1, 1, 1)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    from app.core.db import engine

    dispatched = job_tasks.plan_due_runs(
        database_engine=engine,
        redis_client=redis_client,
        now=datetime(2026, 10, 12, 3, tzinfo=UTC),
    )
    # 只为负责账户的主 BC 筛选；新计划首轮同时做热刷（1 天）与 32 天回刷，
    # 各筛选一次，ad_day 等任务已派发。
    assert calls == ["bc-a", "bc-a"]
    # 登记可能晚于该轮冻结时钟；下一个到期tick必须派发，而非要求创建前时间即可领取。
    assert (
        dispatched
        + job_tasks._dispatch_pending(engine, datetime.now(UTC) + timedelta(seconds=1))
        >= 2
    )


@contextmanager
def _ctx(obj):
    yield obj


def test_low_budget_preserves_realtime_and_pauses_cold(
    monkeypatch, committed_sync_seed, redis_client
):
    from datetime import timedelta

    from app.core.db import engine
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.planner import record_calls
    from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    # 规划时钟晚于入队默认时间，且香港/UTC 均在同一本地日。
    now = datetime.now(UTC).replace(
        hour=12, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks.settings, "TIKTOK_REPORT_DAILY_CALL_BUDGET", 100)
    record_calls(redis_client, now, 75)
    from app.integrations.tiktok.contracts.report_reader import RawReportPage

    calls = []

    class Reader:
        def bc_spend(self, **kwargs):
            calls.append(kwargs)
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    with factory() as session, session.begin():
        cold_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=now.date() - timedelta(days=40),
            end=now.date() - timedelta(days=40),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            due_at=now,
        )
    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_kwargs: _ctx(Reader()))
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.next_hot_at == now + timedelta(minutes=15)
        assert plan.next_material_at == now + timedelta(minutes=15)
        assert plan.next_daily_at == now  # 冷规划仍待执行，不能假装已处理。
        jobs = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
        cold = [job for job in jobs if job.priority >= 3]
        assert [(job.id, job.status) for job in cold] == [(cold_id, "QUEUED")]
        live = [job for job in jobs if job.priority <= 2]
        assert {job.kind for job in live} == {"ad_day", "material_day"}
        assert all(
            job.status == "RUNNING" and job.start_date == job.end_date == now.date()
            for job in live
        )
    assert len(calls) == 1 and calls[0]["start"] == calls[0]["end"] == now.date()


def test_bc_permission_failure_falls_back_next_round(
    monkeypatch, committed_sync_seed, redis_client
):
    from datetime import timedelta

    from app.core.db import engine
    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    calls = []

    class Reader:
        def bc_spend(self, **_kwargs):
            calls.append("bc")
            raise DomainError("report_permission_denied", "报表权限不足")

        def account_day(self, *, advertiser_ids, **_kwargs):
            calls.append(advertiser_ids)
            return RawReportPage((), 1, 0, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_kwargs: _ctx(Reader()))
    now = datetime.now(UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    with factory() as s:
        assert s.get(AdSyncBc, (seed.tenant_id, "bc-a")).bc_report_supported is False
    job_tasks.plan_due_runs(
        database_engine=engine,
        redis_client=redis_client,
        now=now + timedelta(minutes=15),
    )
    # 回退为账户报表：先完成旧热刷，再立即补当前15分钟观察；32天回刷仍为2片。
    from collections import Counter

    assert calls[0] == "bc"
    # 本地日期跨UTC转点时按时区拆批，但每账户的四个观察范围不能丢失。
    assert Counter(account for group in calls[1:] for account in group) == {
        "acc-1": 4,
        "acc-2": 4,
    }
    with factory() as session:
        assert session.get(
            AdSyncBc, (seed.tenant_id, "bc-a")
        ).active_screened_at == now + timedelta(minutes=15)


def test_disabled_worker_never_opens_gateway(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", False)

    def forbidden(**_kwargs):
        raise AssertionError("不允许外部读取")

    monkeypatch.setattr(job_tasks, "_reader_for", forbidden)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "WAITING"
    )


def test_gateway_admission_defers_without_failure(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)

    def deferred(**_kwargs):
        raise AccountAdmissionDeferred(300000)

    monkeypatch.setattr(job_tasks, "_reader_for", deferred)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "WAITING"
    )
    with factory() as s:
        row = s.get(ReportSyncJob, job.id)
        assert row.status == "WAITING" and row.attempts == 0


@pytest.mark.parametrize("fact_age_days", [31, 365])
def test_cleanup_preserves_run_summaries_and_hourly_facts_but_expires_query_snapshots(
    session, sync_seed, fact_age_days
):
    from datetime import timedelta
    from decimal import Decimal
    from uuid import uuid4

    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow
    from app.modules.reporting.store_models import (
        CampaignReportHour,
        ReportBalanceRun,
        ReportSyncJob,
    )
    from tests.modules.reporting.test_jobs import D, _route

    now = datetime.now(UTC)
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="hot",
        advertiser_ids=["acc-1"],
        start=D,
        end=D,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
    )
    job = session.get(ReportSyncJob, job_id)
    job.status = "COMPLETE"
    job.finished_at = now - timedelta(days=8)
    from app.modules.sync_control.schedule import record_report_outcome

    record_report_outcome(session, job)
    snapshot = QuerySnapshot(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        filter_digest="x" * 64,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
        total=1,
    )
    session.add(snapshot)
    session.flush()
    row = QuerySnapshotRow(
        snapshot_id=snapshot.id,
        row_key="old",
        stable_sequence=0,
        dimension="ad",
        membership_digest="x" * 64,
    )
    session.add(row)
    hour = CampaignReportHour(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        stat_hour=now - timedelta(days=fact_age_days),
        spend=Decimal("123.456"),
        revenue_total=Decimal("789.012"),
        campaign_id="c1",
        currency="USD",
        timezone="UTC",
        fetched_at=now,
        sync_job_id=uuid4(),
    )
    session.add(hour)
    session.flush()
    hour_key = (hour.tenant_id, hour.advertiser_id, hour.stat_hour, hour.campaign_id)
    hour_values = hour.model_dump()
    runs = []
    for status in ("COMPLETE", "FAILED", "ACTIVE"):
        run = ReportBalanceRun(
            tenant_id=sync_seed.tenant_id,
            actor_id=sync_seed.actor_id,
            run_key=f"old-{status}",
            frozen_route=_route(sync_seed),
            remaining_advertiser_ids=["acc-1"],
            status=status,
            created_at=now - timedelta(days=8),
        )
        session.add(run)
        runs.append((run.id, status))
    session.flush()
    snapshot_id = snapshot.id
    assert job_tasks.cleanup_store(session, now=now) == {
        "jobs": 0,
        "hours": 0,
        "snapshots": 1,
        "balances": 0,
        "directory_superseded": 0,
        "directory_pages": 0,
        "material_pages": 0,
    }
    session.expire_all()
    assert session.get(QuerySnapshot, snapshot_id) is None
    assert session.get(QuerySnapshotRow, (snapshot_id, "old")) is None
    preserved = session.get(CampaignReportHour, hour_key)
    assert preserved is not None and preserved.model_dump() == hour_values
    assert session.get(ReportSyncJob, job_id) is not None
    for run_id, _status in runs:
        assert session.get(ReportBalanceRun, run_id) is not None


def test_cleanup_removes_only_finished_directory_staging_pages(
    session, sync_seed, monkeypatch
):
    """已发布或作废超过保留期的运行删除暂存页；进行中、失败可重试及近期运行保留。"""
    from datetime import timedelta

    from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
    from tests.modules.reporting.test_jobs import _route

    monkeypatch.setattr(job_tasks, "DIRECTORY_PAGE_CLEANUP_RUNS", 2)
    now = datetime.now(UTC)
    old, recent = now - timedelta(hours=2), now - timedelta(minutes=20)
    cases = {
        "published-old": ("COMPLETE", 1, old, True),
        "cancelled-old": ("CANCELLED", None, old, True),
        "stale-old": ("STALE", None, old, True),
        "published-recent": ("COMPLETE", 1, recent, False),
        "failed-old": ("FAILED", None, old, False),
        "running-old": ("RUNNING", None, old, False),
    }
    runs = {}
    for index, (name, (status, version, at, _)) in enumerate(cases.items()):
        run = AdDirectoryRun(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            connection_id=sync_seed.connection.id,
            channel="OFFICIAL_API",
            frozen_route=_route(sync_seed),
            partition_key=f"{index:x}" * 64,
            query={},
            status=status,
            claim_generation=1,
            published_version=version,
            created_at=at,
            completed_at=at if version else None,
            kind="campaign",
            ad_type="SMART_PLUS",
        )
        session.add(run)
        session.flush()
        for page in (1, 2):
            session.add(
                AdDirectoryPage(
                    run_id=run.id,
                    page=page,
                    tenant_id=sync_seed.tenant_id,
                    advertiser_id="acc-1",
                    claim_generation=1,
                    next_page=None if page == 2 else 2,
                    complete=page == 2,
                    items=[{"id": name}],
                    materials=[],
                )
            )
        runs[name] = run.id
    session.flush()

    def remaining(name):
        return len(
            session.exec(
                select(AdDirectoryPage).where(AdDirectoryPage.run_id == runs[name])
            ).all()
        )

    # 每批最多 2 个运行：三个待清理运行分两批完成，第三批无可清理。
    assert job_tasks.cleanup_store(session, now=now)["directory_pages"] == 4
    assert job_tasks.cleanup_store(session, now=now)["directory_pages"] == 2
    assert job_tasks.cleanup_store(session, now=now)["directory_pages"] == 0
    session.expire_all()
    for name, (*_, removed) in cases.items():
        assert remaining(name) == (0 if removed else 2), name
        # 运行记录本身保留，供目录刷新判断与审计。
        assert session.get(AdDirectoryRun, runs[name]) is not None


@pytest.mark.parametrize("stalled_status", ["QUEUED", "RUNNING", "WAITING_REMOTE"])
def test_superseded_stalled_directory_runs_are_closed_and_cleaned(
    session, sync_seed, stalled_status
):
    """同层级已有更新运行发布后，较早的未完成运行标记为 STALE 并清理暂存页；
    其他广告类型、租约有效、尚无更新发布的运行保持不动。"""
    from datetime import timedelta

    from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
    from tests.modules.reporting.test_jobs import _route

    now = datetime.now(UTC)
    old = now - timedelta(days=3)

    def make(key, status, *, ad_type="SMART_PLUS", version=None, lease=None, at=old):
        run = AdDirectoryRun(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            connection_id=sync_seed.connection.id,
            channel="OFFICIAL_API",
            frozen_route=_route(sync_seed),
            partition_key=key * 64,
            query={},
            status=status,
            claim_generation=1,
            published_version=version,
            claimed_until=lease,
            created_at=at,
            completed_at=at if version else None,
            kind="campaign",
            ad_type=ad_type,
        )
        session.add(run)
        session.flush()
        session.add(
            AdDirectoryPage(
                run_id=run.id,
                page=1,
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                claim_generation=1,
                next_page=2,
                complete=False,
                items=[],
                materials=[],
            )
        )
        session.flush()
        return run.id

    stalled = make("1", stalled_status)
    leased = make("2", "RUNNING", lease=now + timedelta(minutes=5))
    other_type = make("3", "QUEUED", ad_type="NORMAL")
    make("4", "COMPLETE", version=1, at=now - timedelta(minutes=10))
    newest_unpublished = make("5", "RUNNING", at=now - timedelta(hours=2))
    result = job_tasks.cleanup_store(session, now=now)
    assert result["directory_superseded"] == 1
    assert result["directory_pages"] == 1
    session.expire_all()
    row = session.get(AdDirectoryRun, stalled)
    assert (row.status, row.error_code) == ("STALE", "superseded")
    for run_id in (leased, other_type, newest_unpublished):
        assert session.get(AdDirectoryRun, run_id).status in {"RUNNING", "QUEUED"}
        assert session.exec(
            select(AdDirectoryPage).where(AdDirectoryPage.run_id == run_id)
        ).first()


def test_revoked_actor_fails_job_before_gateway(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from app.modules.tenants.models import TenantMembership
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    with factory() as s:
        s.get(TenantMembership, (seed.tenant_id, seed.actor_id)).active = False
        s.commit()
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)

    def forbidden(**_kwargs):
        raise AssertionError("撤权后不得读取")

    monkeypatch.setattr(job_tasks, "_reader_for", forbidden)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "FAILED"
    )


class _EmptyReader:
    def bc_spend(self, **_kwargs):
        from app.integrations.tiktok.contracts.report_reader import RawReportPage

        return RawReportPage((), 1, 0, 0)

    def account_day(self, **_kwargs):
        from app.integrations.tiktok.contracts.report_reader import RawReportPage

        return RawReportPage((), 1, 0, 0)


def _delete_actor(factory, actor_id):
    """测试自建的用户不属于租户夹具清理范围，先删其名下行再删用户。"""
    from sqlmodel import SQLModel

    with factory() as s:
        for table in reversed(SQLModel.metadata.sorted_tables):
            if "actor_id" in table.c:
                s.exec(table.delete().where(table.c.actor_id == actor_id))
        user = SQLModel.metadata.tables["user"]
        s.exec(user.delete().where(user.c.id == actor_id))
        s.commit()


def test_platform_admin_discovery_actor_plans_directory(
    monkeypatch, committed_sync_seed, redis_client
):
    """平台管理员不是租户成员，代租户完成账户发现后，后台目录同步仍以其身份正常规划。"""
    from datetime import timedelta
    from uuid import uuid4

    from sqlmodel import select

    from app.core.db import engine
    from app.models import User
    from app.modules.accounts.models import DiscoveryRun
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    admin = User(
        username=f"admin-{uuid4().hex[:10]}", hashed_password="x", is_superuser=True
    )
    with factory() as s:
        s.add(admin)
        s.flush()
        s.add(
            DiscoveryRun(
                tenant_id=seed.tenant_id,
                actor_id=admin.id,
                connection_id=seed.connection.id,
                bc_id=None,
                status="COMPLETE",
                completed_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        s.commit()
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    try:
        job_tasks.plan_due_runs(
            database_engine=engine,
            redis_client=redis_client,
            now=datetime(2026, 10, 12, 3, tzinfo=UTC),
        )
        with factory() as s:
            assert s.get(AdSyncBc, (seed.tenant_id, "bc-a")).actor_id == admin.id
            runs = s.exec(
                select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
            ).all()
            assert runs and {run.actor_id for run in runs} == {admin.id}
    finally:
        _delete_actor(factory, admin.id)


def test_unexpected_failure_in_one_bc_does_not_block_others(
    monkeypatch, committed_sync_seed, redis_client
):
    """单个 BC 规划出现意外错误时只推迟该 BC，其他 BC 本轮照常规划。"""
    from datetime import timedelta

    from app.core.db import engine
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    with factory() as s:
        s.add(
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-3",
                currency="USD",
                timezone="UTC",
                name="账户三",
            )
        )
        s.flush()
        s.add(
            BCAccountAccess(
                tenant_id=seed.tenant_id,
                bc_id="bc-b",
                advertiser_id="acc-3",
                connection_id=seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=datetime.now(UTC),
            )
        )
        s.commit()
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    original = job_tasks._plan_one

    def flaky(database_engine, redis_client, key, *args, **kwargs):
        if key[1] == "bc-a":
            raise RuntimeError("synthetic planning failure")
        return original(database_engine, redis_client, key, *args, **kwargs)

    monkeypatch.setattr(job_tasks, "_plan_one", flaky)
    now = datetime.now(UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    with factory() as s:
        failed = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        healthy = s.get(AdSyncBc, (seed.tenant_id, "bc-b"))
        assert failed.disabled_reason == "plan_failed"
        assert failed.next_hot_at >= now + timedelta(minutes=5)
        assert healthy.next_hot_at > now and healthy.disabled_reason is None


def test_directory_dispatch_skips_enqueued_runs_and_is_bounded(
    monkeypatch, committed_sync_seed, redis_client
):
    """已写入投递记录的目录运行不再重复入队；每次派发有上限，积压时规划仍保持轻量。"""
    from sqlmodel import Session, func, select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 2)
    now = datetime.now(UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    now = datetime.now(UTC) + timedelta(seconds=1)
    with factory() as registered, registered.begin():
        job_tasks._dispatch_directories(registered, now)

    def queued_runs():
        with factory() as s:
            return s.exec(
                select(func.count())
                .select_from(AdDirectoryRun)
                .where(AdDirectoryRun.tenant_id == seed.tenant_id)
            ).one()

    def enqueued():
        with factory() as s:
            return s.exec(
                select(func.count())
                .select_from(PendingDispatch)
                .where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == "ads.sync_step",
                )
            ).one()

    total = queued_runs()
    assert total > 2
    before = enqueued()
    with Session(engine) as s, s.begin():
        first = job_tasks._dispatch_directories(s, now)
    assert first <= 2
    assert enqueued() == before + first
    # 窗口未腾出前不能继续填 broker；模拟消费者完成后再补位，最终每个运行只投递一次。
    from uuid import UUID

    assert enqueued() <= job_tasks.DIRECTORY_DISPATCH_WINDOW
    for _ in range(total):
        with Session(engine) as s, s.begin():
            dispatched = s.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == "ads.sync_step",
                )
            ).all()
            for dispatch in dispatched:
                run = s.get(AdDirectoryRun, UUID(dispatch.payload["run_id"]))
                _finish_directory_dispatch(s, run, now)
            s.flush()
            job_tasks._dispatch_directories(s, now)
    assert enqueued() == total
    with Session(engine) as s, s.begin():
        assert job_tasks._dispatch_directories(s, now) == 0


@pytest.mark.parametrize("terminal", [False, True])
def test_lost_directory_message_is_redispatched_with_new_generation(
    monkeypatch, committed_sync_seed, redis_client, terminal
):
    """消息发布后长时间无人领取视为丢失：换新代数重新入队，旧消息执行时直接失效。"""
    from datetime import timedelta

    from sqlmodel import Session, select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    _use_dispatch_redis(monkeypatch, redis_client)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    now = datetime.now(UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    now = datetime.now(UTC) + timedelta(seconds=1)
    with factory() as registered, registered.begin():
        job_tasks._dispatch_directories(registered, now)
    with factory() as s:
        from uuid import UUID

        record = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "ads.sync_step",
            )
        ).first()
        assert record is not None, "应先经过真实共享池领取目录任务"
        run = s.get(AdDirectoryRun, UUID(record.payload["run_id"]))
        key = record.task_key
        if terminal:
            from app.modules.ads.sync_models import AdDirectoryPage

            s.add(
                AdDirectoryPage(
                    run_id=run.id,
                    page=1,
                    tenant_id=run.tenant_id,
                    advertiser_id=run.advertiser_id,
                    claim_generation=run.claim_generation,
                    complete=True,
                    items=[],
                    materials=[],
                )
            )
        record.published_at = now - timedelta(minutes=1)
        s.commit()
        run_id, generation = run.id, run.claim_generation
    # 发布不久：仍在等待执行，不重复入队。
    with Session(engine) as s, s.begin():
        job_tasks._dispatch_directories(s, now)
    with factory() as s:
        assert s.get(AdDirectoryRun, run_id).claim_generation == generation
    from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule

    with factory() as s:
        head = s.exec(
            select(SyncSchedule)
            .join(
                SyncExecutionScope, SyncExecutionScope.scope_id == SyncSchedule.scope_id
            )
            .where(SyncExecutionScope.directory_run_id == run_id)
        ).first()
        later = head.leased_until + timedelta(seconds=1)
    # PG使用注入的later，Redis Lua使用自身TIME；让同一批queued租约也经过
    # 相同的240秒。只移动到期时间，保留member交由真实准入Lua自然回收。
    advance_ms = int((later - now).total_seconds() * 1000)
    for redis_key in redis_client.scan_iter(match="*:dispatch"):
        entries = redis_client.zrange(redis_key, 0, -1, withscores=True)
        if entries:
            redis_client.zadd(
                redis_key, {member: score - advance_ms for member, score in entries}
            )
    with Session(engine) as s, s.begin():
        job_tasks._dispatch_directories(s, later)
    with factory() as s:
        run = s.get(AdDirectoryRun, run_id)
        assert run.claim_generation == generation + 1
        dispatch = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key
                == f"ads.sync_step:{run_id}:{generation + 1}:page:{run.next_page}",
            )
        ).one()
        assert dispatch.task_name == (
            "ads.publish_step" if terminal else "ads.sync_step"
        )
        original = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key == key,
            )
        ).one()
        assert original.task_name == "ads.sync_step"


def test_cold_dispatch_window_cannot_block_new_current_day_work(
    monkeypatch, committed_sync_seed
):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from sqlmodel import select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import D, _route

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)

    def enqueue(session, index, tier):
        day = (
            now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
            if tier == "hot"
            else D - timedelta(days=index)
        )
        return enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier=tier,
            advertiser_ids=["acc-1"],
            start=day,
            end=day,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            due_at=now,
        )

    with factory() as session, session.begin():
        for index in range(12):
            enqueue(session, index, "history")
    assert job_tasks._dispatch_pending(engine, now) == 1
    assert job_tasks._dispatch_pending(engine, now) == 0
    with factory() as session, session.begin():
        running = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status == "RUNNING",
            )
        ).all()
        assert len(running) == 1
        cold_ids = {item.id for item in running}
        hot_id = enqueue(session, 20, "hot")
    assert job_tasks._dispatch_pending(engine, now) == 1
    with factory() as session:
        assert session.get(ReportSyncJob, hot_id).status == "RUNNING"

        assert all(
            session.get(ReportSyncJob, cold_id).status == "RUNNING"
            for cold_id in cold_ids
        )
        dispatches = session.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert {(item.task_name, item.payload["job_id"]) for item in dispatches} == {
            *(("reporting.backfill_step", str(cold_id)) for cold_id in cold_ids),
            ("reporting.job_step", str(hot_id)),
        }
    assert job_tasks._dispatch_pending(engine, now) == 0


@pytest.mark.parametrize("refill_failed", [False, True])
def test_report_worker_renews_lease_and_directly_refills_outbox(
    monkeypatch, committed_sync_seed, redis_client, refill_failed
):
    from datetime import timedelta

    from sqlmodel import select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import (
        D,
        FakeReader,
        _enqueue_and_claim,
        _route,
    )

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    claim = job.claim_token
    _use_dispatch_redis(monkeypatch, redis_client)
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        row = session.get(ReportSyncJob, job.id)
        row.claimed_until = now + timedelta(seconds=1)
        next_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D - timedelta(days=1),
            end=D - timedelta(days=1),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )

    @contextmanager
    def reader(**_kwargs):
        with factory() as session:
            assert session.get(ReportSyncJob, job.id).claimed_until >= now + timedelta(
                seconds=230
            )
        yield FakeReader()

    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "_reader_for", reader)

    from sqlalchemy import event

    directory_rounds, directory_sql = [], []
    original_directories = job_tasks._dispatch_directories

    def bounded_directories(*args, **kwargs):
        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            directory_sql.append(" ".join(statement.lower().split()))

        event.listen(engine, "before_cursor_execute", capture)
        try:
            count = original_directories(*args, **kwargs)
            directory_rounds.append(count)
            return count
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    monkeypatch.setattr(job_tasks, "_dispatch_directories", bounded_directories)
    if refill_failed:

        def fail_refill(*_args, **_kwargs):
            raise RuntimeError("temporary refill failure")

        monkeypatch.setattr(job_tasks, "_dispatch_pending", fail_refill)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=claim,
        )
        == "COMPLETE"
    )
    with factory() as session:
        assert session.get(ReportSyncJob, job.id).status == "COMPLETE"
        if refill_failed:
            assert session.get(ReportSyncJob, next_id).status == "QUEUED"
            return
        assert 1 <= len(directory_rounds) <= 8
        head_queries = [
            sql
            for sql in directory_sql
            if "select" in sql
            and "from sync_schedule" in sql
            and "sync_schedule.active" in sql
        ]
        assert head_queries and all(" limit " in sql for sql in head_queries)
        # 统一补位查有界canonical需求头，不按历史消息或全目录run表找工作。
        assert not any("from pending_dispatch" in sql for sql in directory_sql)
        assert session.get(ReportSyncJob, next_id).status == "RUNNING"
        dispatch = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.backfill_step",
                PendingDispatch.payload["job_id"].astext == str(next_id),
            )
        ).one()
        assert dispatch.payload["job_id"] == str(next_id)
        assert (
            dispatch.published_at is None
        )  # 仍由正常 outbox 发布，任务不直发 broker。
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.plan_due",
            )
        ).all()


def _lane_job_scope(now, lane, index):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    # 实时槽用真实账户当地今天；三种报表×两账户提供六个不同的合法任务。
    advertiser = ("acc-1", "acc-2")[index % 2]
    timezone = "Asia/Hong_Kong" if advertiser == "acc-1" else "UTC"
    day = now.astimezone(ZoneInfo(timezone)).date()
    if lane == "backfill":
        return advertiser, "ad_day", day - timedelta(days=index + 1)
    if lane == "links":
        return advertiser, "material_links", day
    if lane == "closeout":
        return (
            advertiser,
            ("ad_day", "material_day")[index % 2],
            day - timedelta(days=1),
        )
    return advertiser, ("ad_day", "material_day", "campaign_hour")[index // 2], day


@pytest.mark.parametrize("lane", ["live", "closeout", "links", "backfill"])
@pytest.mark.parametrize("window", [2, 3])
def test_concurrent_report_dispatchers_cannot_overfill_window(
    monkeypatch, committed_sync_seed, redis_client, request, lane, window
):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import func
    from sqlmodel import select

    from app.core.db import engine
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    _use_dispatch_redis(monkeypatch, redis_client)
    if lane == "backfill":
        _set_background_window(request, window)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    window_name = {
        "live": "REPORT_DISPATCH_WINDOW",
        "links": "REPORT_DISPATCH_WINDOW",
        "closeout": "REPORT_DISPATCH_WINDOW",
        "backfill": "REPORT_DISPATCH_WINDOW",
    }[lane]
    monkeypatch.setattr(job_tasks, window_name, window)
    with factory() as session, session.begin():
        for index in range(6):
            advertiser, kind, day = _lane_job_scope(now, lane, index)
            enqueue_job(
                session,
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                kind=kind,
                campaign_ids=[str(index)]
                if lane in {"links", "closeout", "backfill"}
                else [],
                tier="history"
                if lane == "backfill"
                else "closeout"
                if lane == "closeout"
                else "hot",
                advertiser_ids=[advertiser],
                start=day,
                end=day,
                frozen_route=_route(seed),
                actor_id=seed.actor_id,
                due_at=now,
            )
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(lambda _n: job_tasks._dispatch_pending(engine, now), range(2))
        )
    assert sum(results) == window
    with factory() as session:
        assert (
            session.exec(
                select(func.count())
                .select_from(ReportSyncJob)
                .where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.status == "RUNNING",
                )
            ).one()
            == window
        )


@pytest.mark.parametrize("lane", ["live", "closeout", "links", "backfill"])
def test_single_slot_refill_round_robins_three_tenants_across_rounds(
    monkeypatch, redis_client, request, lane
):
    from contextlib import ExitStack
    from datetime import timedelta

    from sqlmodel import col, select

    from app.core.db import engine
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.sync_conftest import (
        committed_sync_seed as seed_fixture,
    )
    from tests.modules.reporting.test_jobs import _route

    _use_dispatch_redis(monkeypatch, redis_client)
    if lane == "backfill":
        _set_background_window(request, 2)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    # 两槽逐次补一槽，分别验证两条 lane 的跨租户游标而非依赖默认容量。
    monkeypatch.setattr(
        job_tasks,
        {
            "live": "REPORT_DISPATCH_WINDOW",
            "links": "REPORT_DISPATCH_WINDOW",
            "closeout": "REPORT_DISPATCH_WINDOW",
            "backfill": "REPORT_DISPATCH_WINDOW",
        }[lane],
        2,
    )
    with ExitStack() as stack:
        seeds = [
            stack.enter_context(contextmanager(seed_fixture.__wrapped__)())
            for _ in range(3)
        ]
        factory = seeds[0][1]
        now = datetime.now(UTC)
        with factory() as session, session.begin():
            for rank, (seed, _) in enumerate(seeds):
                for index in range(6):
                    advertiser, kind, day = _lane_job_scope(now, lane, index)
                    enqueue_job(
                        session,
                        tenant_id=seed.tenant_id,
                        bc_id="bc-a",
                        kind=kind,
                        campaign_ids=[str(index)]
                        if lane in {"links", "closeout", "backfill"}
                        else [],
                        tier="history"
                        if lane == "backfill"
                        else "closeout"
                        if lane == "closeout"
                        else "hot",
                        advertiser_ids=[advertiser],
                        start=day,
                        end=day,
                        frozen_route=_route(seed),
                        actor_id=seed.actor_id,
                        due_at=now - timedelta(minutes=3 - rank),
                    )
        assert job_tasks._dispatch_pending(engine, now) == 2
        with factory() as session:
            running = session.exec(
                select(ReportSyncJob)
                .where(ReportSyncJob.status == "RUNNING")
                .order_by(ReportSyncJob.due_at, ReportSyncJob.id)
            ).all()
            active = [r.id for r in running]
            seen = [r.tenant_id for r in running]
        for round_no in range(6):
            finished = active.pop(0)
            with factory() as session, session.begin():
                row = session.get(ReportSyncJob, finished)
                _finish_report_dispatch(session, row, now)
            assert (
                job_tasks._dispatch_pending(
                    engine, now + timedelta(seconds=round_no + 1)
                )
                == 1
            )
            with factory() as session:
                fresh = session.exec(
                    select(ReportSyncJob).where(
                        ReportSyncJob.status == "RUNNING",
                        col(ReportSyncJob.id).not_in(active),
                    )
                ).one()
                active.append(fresh.id)
                seen.append(fresh.tenant_id)
        tenants = {s.tenant_id for s, _ in seeds}
        from collections import Counter

        # 同批两次派发时间相同，不承诺它们的全局FIFO次序；跨轮游标仍须保证
        # 累计份额相差至多一次，任意连续四次均覆盖三个租户，不能饿死小租户。
        counts = Counter(seen)
        assert (
            set(counts) == tenants and max(counts.values()) - min(counts.values()) <= 1
        )
        assert all(
            set(seen[offset : offset + 4]) == tenants for offset in range(len(seen) - 3)
        )


@pytest.mark.parametrize("missing_lease", [False, True])
def test_expired_broker_claim_cannot_bypass_dispatch_window(
    monkeypatch, committed_sync_seed, redis_client, missing_lease
):
    """上线前积压的过期消息不能自行续租，绕过新窗口和热任务优先级。"""
    from datetime import timedelta

    from app.core.db import engine
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    with factory() as session, session.begin():
        session.get(ReportSyncJob, job.id).claimed_until = (
            None if missing_lease else datetime.now(UTC) - timedelta(seconds=1)
        )
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)

    def forbidden(**_kwargs):
        raise AssertionError("过期消息须由有界调度重新领取，不能自行恢复执行")

    monkeypatch.setattr(job_tasks, "_reader_for", forbidden)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "FAILED"
    )
    with factory() as session:
        row = session.get(ReportSyncJob, job.id)
        assert row.status == "RUNNING"
        assert row.claimed_until is None or row.claimed_until < datetime.now(UTC)
        assert row.finished_at is None

    from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule

    with factory() as session, session.begin():
        for head in session.exec(
            select(SyncSchedule)
            .join(
                SyncExecutionScope, SyncExecutionScope.scope_id == SyncSchedule.scope_id
            )
            .where(SyncExecutionScope.report_job_id == job.id)
        ).all():
            head.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        job_tasks._dispatch_report_jobs(session, datetime.now(UTC))
    with factory() as session:
        row = session.get(ReportSyncJob, job.id)
        assert row.claim_token != job.claim_token and row.claimed_until > datetime.now(
            UTC
        )
    # 新代数已经领取后，旧消息仍不可进入平台；不改变新代数的有效租约。
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "FAILED"
    )


def test_directory_dispatch_window_does_not_fill_broker_on_repeated_scans(
    session, sync_seed, monkeypatch
):
    from app.core.config import settings
    from app.jobs.models import PendingDispatch
    from app.modules.ads.directory_requests import request_directory
    from app.modules.ads.sync_models import AdDirectoryRun
    from tests.modules.ads.test_directory_requests import _route

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 2, raising=False)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="directory",
    )
    now = datetime.now(UTC)
    job_tasks._dispatch_directories(session, now)
    job_tasks._dispatch_directories(session, now)
    dispatches = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == "ads.sync_step",
        )
    ).all()
    assert len(dispatches) == 2
    first_id = dispatches[0].payload["run_id"]
    from uuid import UUID

    completed = session.get(AdDirectoryRun, UUID(first_id))
    _finish_directory_dispatch(session, completed, now)
    session.flush()
    job_tasks._dispatch_directories(session, now)
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == sync_seed.tenant_id,
                    PendingDispatch.task_name == "ads.sync_step",
                )
            ).all()
        )
        == 3
    )


def test_expired_claim_frees_directory_window_slot(session, sync_seed, monkeypatch):
    """消息已被领取、租约过期的运行不再占窗口；否则 6 小时内窗口永久满，全量目录停摆。"""
    from datetime import timedelta
    from uuid import UUID

    from app.core.config import settings
    from app.jobs.models import PendingDispatch
    from app.modules.ads.directory_requests import request_directory
    from app.modules.ads.sync_models import AdDirectoryRun
    from tests.modules.ads.test_directory_requests import _route

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 2, raising=False)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="directory",
    )
    now = datetime.now(UTC)
    job_tasks._dispatch_directories(session, now)

    def sync_dispatches():
        return session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == sync_seed.tenant_id,
                PendingDispatch.task_name == "ads.sync_step",
            )
        ).all()

    first = sync_dispatches()
    assert len(first) == 2
    # 两条消息都已发布并被 Worker 领取，Worker 随后中断，租约已过期。
    for dispatch in first:
        dispatch.published_at = now - timedelta(minutes=30)
        run = session.get(AdDirectoryRun, UUID(dispatch.payload["run_id"]))
        run.status = "RUNNING"
        run.claimed_until = now - timedelta(minutes=5)
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 2


def test_object_directory_runs_dispatch_before_older_creative_scans(
    session, sync_seed, monkeypatch
):
    """素材目录每次十余页，旧素材积压不能占满窗口，饿死止损依赖的系列/组/广告目录。"""
    from datetime import timedelta
    from uuid import UUID

    from app.core.config import settings
    from app.jobs.models import PendingDispatch
    from app.modules.ads.directory_requests import request_directory
    from app.modules.ads.sync_models import AdDirectoryRun
    from tests.modules.ads.test_directory_requests import _route

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 1, raising=False)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="directory",
    )
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {run.kind for run in runs} >= {"creative", "ad"}
    now = datetime.now(UTC)
    for run in runs:
        # 素材目录最旧，其余对象目录更新。
        run.created_at = now - timedelta(days=3 if run.kind == "creative" else 1)
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 1
    dispatched = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == "ads.sync_step",
        )
    ).one()
    assert session.get(AdDirectoryRun, UUID(dispatched.payload["run_id"])).kind != (
        "creative"
    )


def test_concurrent_directory_scans_share_one_dispatch_window(
    monkeypatch, committed_sync_seed
):
    from concurrent.futures import ThreadPoolExecutor

    from app.jobs.models import PendingDispatch
    from app.modules.ads.directory_requests import request_directory
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 2)
    with factory() as session, session.begin():
        request_directory(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            advertiser_ids=["acc-1"],
            scope="directory",
        )

    def scan(_n):
        with factory() as session, session.begin():
            return job_tasks._dispatch_directories(session, datetime.now(UTC))

    with ThreadPoolExecutor(2) as pool:
        counts = list(pool.map(scan, range(2)))
    assert sum(counts) == 2
    with factory() as session:
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == seed.tenant_id,
                        PendingDispatch.task_name == "ads.sync_step",
                    )
                ).all()
            )
            == 2
        )


def test_directory_query_cancel_preserves_hot_and_material_jobs(
    monkeypatch, committed_sync_seed, redis_client
):
    from datetime import timedelta

    from sqlalchemy import text

    from app.core.db import engine
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting import planner
    from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)

    class Reader:
        def bc_spend(self, **_kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    timeout_seen = []

    def cancelled_directory_query(session, **_kwargs):
        timeout_seen.append(
            session.connection().execute(text("SHOW statement_timeout")).scalar_one()
        )
        # 真 PostgreSQL 查询取消，使事务进入失败状态；不靠等待或假异常模拟回滚。
        session.connection().execute(text("SELECT pg_cancel_backend(pg_backend_pid())"))
        session.connection().execute(text("SELECT 1"))

    monkeypatch.setattr(job_tasks, "directory_ad_types", cancelled_directory_query)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now,
        now + timedelta(seconds=20),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    assert len(timeout_seen) == 1
    timeout = timeout_seen[0]
    timeout_ms = (
        float(timeout[:-2]) if timeout.endswith("ms") else float(timeout[:-1]) * 1000
    )
    assert 0 < timeout_ms <= job_tasks.DIRECTORY_PLAN_SECONDS * 1000
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.next_hot_at > now and plan.next_material_at > now
        assert plan.next_directory_at > now
        assert plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]["after"] == ""
        assert plan.disabled_reason is None
        kinds = set(
            session.exec(
                select(ReportSyncJob.kind).where(
                    ReportSyncJob.tenant_id == seed.tenant_id
                )
            ).all()
        )
        assert {"ad_day", "material_day"} <= kinds
        assert (
            session.connection().execute(text("SHOW statement_timeout")).scalar_one()
            == "0"
        )


def test_pending_full_directory_skips_object_scan_and_replanning(
    monkeypatch, committed_sync_seed, redis_client
):
    from datetime import timedelta

    from sqlalchemy import event

    from app.core.db import engine
    from app.modules.ads.directory_requests import request_directory
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting import planner
    from app.modules.reporting.store_models import AdSyncBc
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_hot_at = plan.next_material_at = plan.next_daily_at = now + timedelta(
            hours=1
        )
        plan.last_active_advertiser_ids = ["acc-1", "acc-2"]
        assert (
            request_directory(
                session,
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                actor_id=seed.actor_id,
                frozen_route=_route(seed),
                advertiser_ids=["acc-1", "acc-2"],
                scope="directory",
            )
            == 20
        )

    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        job_tasks._plan_one(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now,
            now + timedelta(seconds=20),
            planner.HOT_INTERVAL,
            planner.MATERIAL_INTERVAL,
            allow_backfill=False,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any("FROM ad_object" in statement for statement in statements)
    with factory() as session:
        runs = session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).all()
        assert len(runs) == 20 and {run.status for run in runs} == {"QUEUED"}
        assert session.get(AdSyncBc, (seed.tenant_id, "bc-a")).next_directory_at > now


def test_link_backlog_cannot_consume_fact_or_cold_windows(
    monkeypatch, committed_sync_seed
):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from app.core.db import engine
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 4)
    with factory() as session, session.begin():
        common = {
            "tenant_id": seed.tenant_id,
            "bc_id": "bc-a",
            "advertiser_ids": ["acc-1"],
            "frozen_route": _route(seed),
            "actor_id": seed.actor_id,
        }
        for index in range(6):
            day = today - timedelta(days=index)
            enqueue_job(
                session,
                **common,
                kind="material_links",
                tier="material",
                start=day,
                end=day,
                due_at=now - timedelta(hours=1),
            )
        first = enqueue_job(
            session,
            **common,
            kind="ad_day",
            tier="hot",
            start=today,
            end=today,
            due_at=now,
        )
        second = enqueue_job(
            session,
            **common,
            kind="material_day",
            tier="material",
            start=today,
            end=today,
            due_at=now,
        )
        cold = enqueue_job(
            session,
            **common,
            kind="ad_day",
            tier="history",
            start=today - timedelta(days=40),
            end=today - timedelta(days=40),
            due_at=now,
        )
    assert job_tasks._dispatch_pending(engine, now) == 4
    with factory() as session, session.begin():
        running = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status == "RUNNING",
            )
        ).all()
        assert {row.kind for row in running} == {
            "ad_day",
            "material_day",
            "material_links",
        }
        assert session.get(ReportSyncJob, first).status == "RUNNING"
        assert session.get(ReportSyncJob, cold).status == "RUNNING"
        _finish_report_dispatch(session, session.get(ReportSyncJob, first), now)
    assert job_tasks._dispatch_pending(engine, now) == 0
    with factory() as session:
        assert session.get(ReportSyncJob, second).status == "RUNNING"
    assert job_tasks._dispatch_pending(engine, now) == 0


@pytest.mark.parametrize(
    "historical_running,missing_timezone",
    [(False, False), (True, False), (False, True)],
)
def test_low_budget_links_only_claim_current_day_with_shared_capacity(
    monkeypatch, committed_sync_seed, historical_running, missing_timezone
):
    from datetime import timedelta
    from uuid import uuid4
    from zoneinfo import ZoneInfo

    from app.core.db import engine
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 1)
    with factory() as session, session.begin():
        common = {
            "tenant_id": seed.tenant_id,
            "bc_id": "bc-a",
            "actor_id": seed.actor_id,
            "frozen_route": _route(seed),
            "kind": "material_links",
            "tier": "material",
            "advertiser_ids": ["acc-1"],
        }
        old = enqueue_job(
            session,
            **common,
            start=today - timedelta(days=1),
            end=today - timedelta(days=1),
            due_at=now - timedelta(hours=1),
        )
        current = enqueue_job(session, **common, start=today, end=today, due_at=now)
        if historical_running:
            row = session.get(ReportSyncJob, old)
            row.status, row.claim_token, row.claimed_until = (
                "RUNNING",
                uuid4(),
                now + timedelta(minutes=4),
            )
        if missing_timezone:
            session.get(AdvertiserAccount, (seed.tenant_id, "acc-1")).timezone = ""
    first_count = job_tasks._dispatch_pending(engine, now, allow_backfill=False)
    assert first_count == (0 if missing_timezone or historical_running else 1)
    with factory() as session, session.begin():
        assert session.get(ReportSyncJob, old).status == (
            "RUNNING" if historical_running else "QUEUED"
        )
        assert session.get(ReportSyncJob, current).status == (
            "QUEUED" if missing_timezone or historical_running else "RUNNING"
        )
        if historical_running:
            _finish_report_dispatch(session, session.get(ReportSyncJob, old), now)
    if historical_running:
        assert job_tasks._dispatch_pending(engine, now, allow_backfill=False) == 1
        with factory() as session:
            assert session.get(ReportSyncJob, current).status == "RUNNING"
    assert job_tasks._dispatch_pending(engine, now, allow_backfill=False) == 0


@pytest.mark.parametrize("full_slots,targeted_slots", [(2, 1), (0, 1), (2, 0)])
def test_directory_dispatch_loads_only_available_candidates(
    session, sync_seed, monkeypatch, full_slots, targeted_slots
):
    """同scope的60个冻结执行只取可用头，不将全部历史搬进ORM。"""
    from sqlalchemy import event

    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.directory_demand import register_directory_run
    from tests.modules.reporting.test_jobs import _route

    now = datetime.now(UTC)
    monkeypatch.setattr(
        job_tasks, "DIRECTORY_DISPATCH_WINDOW", full_slots + targeted_slots
    )
    tenant_id = sync_seed.tenant_id
    for index in range(60):
        run = AdDirectoryRun(
            tenant_id=tenant_id,
            advertiser_id="acc-1",
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            connection_id=sync_seed.connection.id,
            frozen_route=_route(sync_seed),
            partition_key=f"{index:064x}",
            query={"ids": [f"ad-{index}"]} if index < 30 else {},
            status="QUEUED",
            kind="ad",
            ad_type="SMART_PLUS",
            created_at=now - timedelta(seconds=60 - index),
            next_attempt_at=now,
        )
        session.add(run)
        session.flush()
        register_directory_run(session, run)
    session.flush()
    session.expunge_all()
    loaded = []

    def track_load(_session, row):
        if isinstance(row, AdDirectoryRun):
            loaded.append(row.id)

    event.listen(session, "loaded_as_persistent", track_load)
    try:
        assert job_tasks._dispatch_directories(session, now) == 1
    finally:
        event.remove(session, "loaded_as_persistent", track_load)
    assert len(loaded) == 1
    messages = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == tenant_id,
            PendingDispatch.task_name.in_(["ads.sync_step", "ads.targeted_step"]),
        )
    ).all()
    assert len(messages) == 1 and messages[0].task_name == "ads.targeted_step"
    assert job_tasks._dispatch_directories(session, now) == 0


@pytest.mark.parametrize("batch", [1, 2])
def test_bounded_directory_candidates_preserve_tenant_turns(monkeypatch, batch):
    from contextlib import ExitStack

    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.directory_demand import register_directory_run
    from tests.modules.reporting.sync_conftest import (
        committed_sync_seed as seed_fixture,
    )
    from tests.modules.reporting.test_jobs import _route

    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", batch)
    with ExitStack() as stack:
        seeds = [
            stack.enter_context(contextmanager(seed_fixture.__wrapped__)())
            for _ in range(2)
        ]
        factory, now = seeds[0][1], datetime.now(UTC)
        with factory() as session, session.begin():
            for rank, (seed, _) in enumerate(seeds):
                for index in range(4):
                    run = AdDirectoryRun(
                        tenant_id=seed.tenant_id,
                        advertiser_id="acc-1",
                        bc_id="bc-a",
                        actor_id=seed.actor_id,
                        connection_id=seed.connection.id,
                        frozen_route=_route(seed),
                        partition_key=f"{index:064x}",
                        query={"ids": [f"ad-{index}"]},
                        status="QUEUED",
                        kind="ad",
                        ad_type="SMART_PLUS",
                        created_at=now - timedelta(seconds=20 - rank * 5 - index),
                        next_attempt_at=now,
                    )
                    session.add(run)
                    session.flush()
                    register_directory_run(session, run)
        with factory() as session, session.begin():
            assert job_tasks._dispatch_directories(session, now) == batch
        if batch == 1:
            with factory() as session, session.begin():
                from uuid import UUID

                first_message = session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.task_name.in_(
                            ["ads.sync_step", "ads.targeted_step"]
                        )
                    )
                ).one()
                first_run = session.get(
                    AdDirectoryRun, UUID(first_message.payload["run_id"])
                )
                _finish_directory_dispatch(session, first_run, now)
                assert (
                    job_tasks._dispatch_directories(session, now + timedelta(seconds=1))
                    == 1
                )
        with factory() as session:
            rows = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name.in_(
                        ["ads.sync_step", "ads.targeted_step"]
                    )
                )
            ).all()
            assert len(rows) == 2 and {r.tenant_id for r in rows} == {
                seed.tenant_id for seed, _ in seeds
            }


@pytest.mark.parametrize("different_route", [False, True])
def test_duplicate_full_creative_scan_finishes_newest_exact_scope_first(
    session, sync_seed, monkeypatch, different_route
):
    from datetime import timedelta
    from uuid import UUID, uuid4

    from app.jobs.models import PendingDispatch
    from app.modules.ads.directory_requests import request_directory
    from app.modules.ads.sync_models import AdDirectoryRun
    from tests.modules.ads.test_directory_requests import _route

    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 1)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="directory",
        ad_types={"acc-1": {"SMART_PLUS": ["creative"]}},
    )
    older = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).one()
    older.status = "RUNNING"
    older.claimed_until = datetime.now(UTC) - timedelta(minutes=2)
    older.created_at = datetime.now(UTC) - timedelta(days=2)
    session.flush()
    newer = AdDirectoryRun(
        **{
            **older.model_dump(exclude={"id", "request_sequence", "request_id"}),
            "id": uuid4(),
            "request_id": uuid4(),
            "status": "QUEUED",
            "claimed_until": None,
            "created_at": datetime.now(UTC),
            "frozen_route": {**older.frozen_route, "binding_revision": 99}
            if different_route
            else older.frozen_route,
        }
    )
    session.add(newer)
    session.flush()
    from app.modules.sync_control.directory_demand import register_directory_run

    register_directory_run(session, newer)
    assert newer.request_sequence > older.request_sequence
    assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 1
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "ads.sync_step",
            PendingDispatch.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    assert UUID(dispatch.payload["run_id"]) == older.id
    # 只调整执行顺序，旧任务和分页未伪报成功、未删除。
    session.refresh(older)
    assert older.status == "RUNNING" and older.published_version is None


@pytest.mark.parametrize("failure_stage", ["open", "hot", "daily", "backoff"])
def test_screening_deferral_cannot_skip_due_live_reports(
    monkeypatch, committed_sync_seed, redis_client, failure_stage
):
    """已知活跃账户的广告和素材刷新独立于账户发现与历史筛选。"""
    from datetime import timedelta

    from app.core.db import engine
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.reporting import planner
    from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as s, s.begin():
        planner.ensure_bc_plans(s, now=now)
        plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = ["acc-1", "acc-2"]
        plan.next_directory_at = now + timedelta(hours=1)
        if failure_stage == "backoff":
            plan.next_hot_at = plan.next_daily_at = now + timedelta(minutes=5)

    class Reader:
        def bc_spend(self, *, start, end, **_kwargs):
            if failure_stage == "hot" or failure_stage == "daily" and start < end:
                raise AccountAdmissionDeferred(300000)
            return RawReportPage(
                tuple(
                    {"dimensions": {"advertiser_id": a}, "metrics": {"spend": "1"}}
                    for a in ("acc-1", "acc-2")
                ),
                1,
                1,
                2,
            )

    @contextmanager
    def reader(**_kwargs):
        assert failure_stage != "backoff", "账户筛选仍应遵守退避时间"
        if failure_stage == "open":
            raise AccountAdmissionDeferred(300000)
        yield Reader()

    monkeypatch.setattr(job_tasks, "_reader_for", reader)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now,
        now + timedelta(seconds=20),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
    )
    with factory() as s:
        jobs = s.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.kind == "material_day",
            )
        ).all()
        assert {tuple(j.advertiser_ids) for j in jobs} == {("acc-1",), ("acc-2",)}
        ads = s.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.kind == "ad_day",
                ReportSyncJob.priority == 1,
            )
        ).all()
        assert {a for j in ads for a in j.advertiser_ids} == (
            set() if failure_stage == "backoff" else {"acc-1", "acc-2"}
        )
        plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert (
            plan.next_material_at
            == plan.next_hot_at
            == now + timedelta(minutes=5 if failure_stage == "backoff" else 15)
        )
        assert plan.next_daily_at >= now + timedelta(minutes=5)


@pytest.mark.parametrize("screen_state", ["success", "backoff", "recent", "expired"])
@pytest.mark.parametrize(
    "rule_bc,enabled", [("bc-a", True), ("bc-b", True), ("bc-a", False)]
)
def test_screening_backoff_does_not_expand_refresh_to_idle_rule_accounts(
    monkeypatch, committed_sync_seed, redis_client, rule_bc, enabled, screen_state
):
    from datetime import timedelta

    from app.core.db import engine
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.reporting import planner
    from app.modules.reporting.store_models import (
        AdSyncBc,
        ReportSyncJob,
        ReportSyncState,
    )

    screen_ok = screen_state == "success"
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as s, s.begin():
        planner.ensure_bc_plans(s, now=now)
        plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = ["acc-1"]
        plan.next_daily_at = plan.next_directory_at = now + timedelta(minutes=5)
        plan.next_hot_at = now if screen_ok else now + timedelta(minutes=5)
        if screen_state in {"recent", "expired"}:
            plan.active_screened_at = now - timedelta(
                minutes=1 if screen_state == "recent" else 16
            )
            for kind in ("ad_day", "material_day", "account_day"):
                s.add(
                    ReportSyncState(
                        tenant_id=seed.tenant_id,
                        advertiser_id="acc-2",
                        report_kind=kind,
                        stat_date=now.date(),
                        status="COMPLETE",
                        row_count=0,
                        last_success_at=plan.active_screened_at,
                    )
                )
        s.add(
            AutomationRule(
                tenant_id=seed.tenant_id,
                name="规则空账户仍须真实刷新",
                created_by=seed.actor_id,
                updated_by=seed.actor_id,
                enabled=enabled,
                definition={"accounts": {"mode": "BC", "bc_ids": [rule_bc]}},
            )
        )

    class Reader:
        def bc_spend(self, **_kwargs):
            from app.integrations.tiktok.contracts.report_reader import RawReportPage

            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    def unexpected_reader(**_kwargs):
        assert screen_ok, "仍须尊重账户发现的退避"
        return _ctx(Reader())

    monkeypatch.setattr(job_tasks, "_reader_for", unexpected_reader)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now,
        now + timedelta(seconds=20),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    with factory() as s:
        jobs = s.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
        for kind in ("ad_day", "material_day"):
            expected = {"acc-1"} if screen_ok or kind == "material_day" else set()
            assert {
                a for j in jobs if j.kind == kind for a in j.advertiser_ids
            } == expected
        from app.modules.reporting.health import measure_health

        metrics, _, _ = measure_health(
            s, s.get(AdSyncBc, (seed.tenant_id, "bc-a")), now=now
        )
        assert (
            metrics["stale_ad_accounts"]
            == metrics["stale_material_accounts"]
            == (len(expected) if screen_state == "backoff" else 1)
        )


def test_default_does_not_suspend_history_using_aggregate_endpoint_calls(
    redis_client, monkeypatch
):
    from app.modules.reporting.planner import backfill_allowed, record_calls

    monkeypatch.setattr(job_tasks.settings, "TIKTOK_REPORT_DAILY_CALL_BUDGET", 0)
    now = datetime.now(UTC)
    record_calls(redis_client, now, 900000)
    assert backfill_allowed(redis_client, now)


@pytest.mark.parametrize("material_offset", [-7, 7])
def test_offset_live_clocks_do_not_duplicate_hot_rounds(
    monkeypatch, committed_sync_seed, redis_client, material_offset
):
    """即使完成任务已退出去重范围，素材错相也不能再请求本轮广告日报。"""
    from datetime import timedelta

    from app.core.db import engine
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting import planner
    from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob

    seed, factory = committed_sync_seed
    now = datetime(2026, 10, 21, 4, tzinfo=UTC)
    with factory() as s, s.begin():
        planner.ensure_bc_plans(s, now=now)
        plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_material_at = now + timedelta(minutes=material_offset)
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
        plan.last_active_advertiser_ids = ["acc-1"]

    class Reader:
        def bc_spend(self, **_kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_kwargs: _ctx(Reader()))
    for minute in (0, 7, 15, 22, 30):
        at = now + timedelta(minutes=minute)
        job_tasks._plan_one(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            at,
            at + timedelta(seconds=20),
            planner.HOT_INTERVAL,
            planner.MATERIAL_INTERVAL,
            allow_backfill=False,
        )
        with factory() as s, s.begin():
            jobs = s.exec(
                select(ReportSyncJob).where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.kind == "ad_day",
                )
            ).all()
            assert len(jobs) == minute // 15 + 1
            for job in s.exec(
                select(ReportSyncJob).where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                )
            ).all():
                job.status = "COMPLETE"
            plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            assert plan.next_material_at == plan.next_hot_at
            assert plan.next_hot_at == now + timedelta(minutes=(minute // 15 + 1) * 15)


def _finish_report_dispatch(session, row, _now):
    """模拟消费者完成时也结算持久头和自己的queued占位，不只改父任务状态。"""
    from redis import Redis

    from app.core.config import settings
    from app.modules.sync_control.occupancy import release_dispatch
    from app.modules.sync_control.schedule import record_report_outcome, report_endpoint

    token = row.claim_token
    row.status = "COMPLETE"
    row.claim_token = row.claimed_until = None
    record_report_outcome(session, row)
    if token:
        endpoint = report_endpoint(row.kind)
        with Redis.from_url(settings.REDIS_URL) as redis:
            release_dispatch(
                redis,
                reservation_id=token,
                app_scope=settings.TIKTOK_APP_ID,
                endpoint=endpoint,
                rate_group=endpoint,
                tenant_id=row.tenant_id,
                advertiser_id=row.advertiser_ids[0],
            )


def _finish_directory_dispatch(session, run, _now):
    from redis import Redis

    from app.core.config import settings
    from app.modules.ads.directory_dispatch import directory_step_key
    from app.modules.sync_control.directory_demand import (
        directory_endpoint,
        directory_outcome,
    )
    from app.modules.sync_control.occupancy import release_dispatch

    endpoint = directory_endpoint(run)
    with Redis.from_url(settings.REDIS_URL) as redis:
        release_dispatch(
            redis,
            reservation_id=directory_step_key(run),
            app_scope=settings.TIKTOK_APP_ID,
            endpoint=endpoint,
            rate_group=endpoint,
            tenant_id=run.tenant_id,
            advertiser_id=run.advertiser_id,
        )
    run.status = "COMPLETE"
    run.claim_token = run.claimed_until = None
    directory_outcome(session, run)
