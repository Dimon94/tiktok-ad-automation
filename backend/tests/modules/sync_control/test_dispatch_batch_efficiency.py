"""真实PG/Redis验证有界批量派发的固定查询开销与回滚边界。"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.core.db import engine
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_jobs import _route


def seed_jobs(factory, seed, count):
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        jobs = []
        for n in range(count):
            account = f"batch-{n}"
            db.add(
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id=account,
                    currency="USD",
                    timezone="UTC",
                    name=account,
                )
            )
            db.flush()
            db.add(
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
            db.flush()
            jobs.append(
                enqueue_job(
                    db,
                    tenant_id=seed.tenant_id,
                    bc_id="bc-a",
                    kind="ad_day",
                    tier="hot",
                    advertiser_ids=[account],
                    start=now.date(),
                    end=now.date(),
                    frozen_route=_route(seed),
                    actor_id=seed.actor_id,
                    due_at=now,
                )
            )
    return jobs, now + timedelta(seconds=1)


@pytest.mark.usefixtures("redis_client")
def test_ready_account_batch_does_not_repeat_setup_for_every_claim(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    jobs, now = seed_jobs(factory, seed, 10)
    setups = []
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)
        if "SELECT setting FROM pg_settings" in sql:
            setups.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        claimed = job_tasks._dispatch_pending(engine, now, budget_seconds=20)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    with factory() as db:
        running = db.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.id.in_(jobs), ReportSyncJob.status == "RUNNING"
            )
        ).all()
    assert claimed == len(running) == 10
    # 固定控制面检查应按小批次摊销，不能每领取一条重扫三个家族。
    assert len(setups) <= 6, (len(setups), len(statements))
    assert len({job.claim_token for job in running}) == 10


def test_failure_inside_batch_rolls_back_all_claims_and_redis_reservations(
    committed_sync_seed, redis_client, monkeypatch
):
    from app.jobs.models import PendingDispatch
    from app.modules.reporting.query_budget import QueryBudgetExceeded
    from app.modules.sync_control import dispatcher
    from app.modules.sync_control.occupancy import occupancy_keys
    from app.modules.sync_control.schedule import report_endpoint

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    jobs, now = seed_jobs(factory, seed, 4)
    original = dispatcher.reserve_execution_cost
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise QueryBudgetExceeded()
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "reserve_execution_cost", fail_second)
    assert job_tasks._dispatch_pending(engine, now, budget_seconds=4) == 0
    with factory() as db:
        assert all(db.get(ReportSyncJob, job).status == "QUEUED" for job in jobs)
        assert not db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.job_step",
            )
        ).all()
    endpoint = report_endpoint("ad_day")
    for n in range(4):
        keys = occupancy_keys(
            app_scope=job_tasks.settings.TIKTOK_APP_ID,
            endpoint=endpoint,
            rate_group=endpoint,
            tenant_id=seed.tenant_id,
            advertiser_id=f"batch-{n}",
        )
        assert all(redis_client.zcard(key) == 0 for key in keys)


@pytest.mark.usefixtures("redis_client", "committed_sync_seed")
def test_worker_refill_without_explicit_budget_still_bounds_transaction(monkeypatch):
    from sqlalchemy import text

    from app.modules.reporting.query_budget import QueryBudgetExceeded

    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    timeouts = []

    def inspect_budget(session, *_args, **_kwargs):
        timeouts.append(
            int(
                session.execute(
                    text(
                        "SELECT setting FROM pg_settings WHERE name='statement_timeout'"
                    )
                ).scalar_one()
            )
        )
        raise QueryBudgetExceeded()

    monkeypatch.setattr(job_tasks, "_dispatch_report_jobs", inspect_budget)
    assert job_tasks._dispatch_pending(engine, datetime.now(UTC)) == 0
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 4000


@pytest.mark.usefixtures("redis_client")
def test_report_candidate_selection_amortizes_class_probes(
    committed_sync_seed, monkeypatch
):
    """十个真实账户的领取不能为每个工作类逐一往返数据库。"""
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    jobs, now = seed_jobs(factory, seed, 10)
    candidate_reads = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if (
            sql.startswith("SELECT")
            and "sync_schedule.scope_id, sync_schedule.work_class" in sql
            and "sync_schedule.last_dispatched_at ASC" in sql
        ):
            candidate_reads.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        claimed = job_tasks._dispatch_pending(engine, now, budget_seconds=20)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    with factory() as db:
        running = [db.get(ReportSyncJob, identity) for identity in jobs]
        assert all(job.status == "RUNNING" for job in running)
        assert len({job.claim_token for job in running}) == 10
    assert claimed == 10
    # 允许最终空探测和有界提交；成本须按账户摊销，不能乘四个工作类。
    assert len(candidate_reads) <= 16, len(candidate_reads)


def test_mixed_report_classes_keep_oldest_head_and_frozen_weights(
    session, sync_seed, redis_client
):
    from datetime import timedelta

    from app.modules.sync_control.dispatcher import dispatch_due
    from app.modules.sync_control.fairness import WEIGHTS
    from app.modules.sync_control.models import SyncCostLedger, SyncSchedule, SyncScope

    now = datetime.now(UTC)
    jobs = {}
    for tier, offset in [("hot", 0), ("history", 5)]:
        day = now.date() - timedelta(days=offset)
        for account in ["acc-1", "acc-2"]:
            jobs[tier, account] = enqueue_job(
                session,
                tenant_id=sync_seed.tenant_id,
                bc_id="bc-a",
                kind="ad_day",
                tier=tier,
                advertiser_ids=[account],
                start=day,
                end=day,
                frozen_route=_route(sync_seed),
                actor_id=sync_seed.actor_id,
                due_at=now,
            )
    for head, scope in session.exec(
        select(SyncSchedule, SyncScope)
        .join(SyncScope, SyncScope.id == SyncSchedule.scope_id)
        .where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).all():
        head.last_dispatched_at = now - timedelta(
            days=2 if scope.advertiser_id == "acc-1" else 1
        )
    assert (
        dispatch_due(session, redis_client, now=now, physical_slots=2, max_claims=2)
        == 2
    )
    running = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.tenant_id == sync_seed.tenant_id,
            ReportSyncJob.status == "RUNNING",
        )
    ).all()
    assert {job.id for job in running} == {
        jobs["hot", "acc-1"],
        jobs["history", "acc-1"],
    }
    ledgers = session.exec(
        select(SyncCostLedger).where(SyncCostLedger.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(ledgers) == 2
    assert {row.work_class for row in ledgers} == {"realtime", "history"}
    assert all(
        row.eligible_weights == {key: WEIGHTS[key] for key in ["realtime", "history"]}
        for row in ledgers
    )
