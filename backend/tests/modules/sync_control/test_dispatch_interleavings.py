"""真实PG交错：实时窗口不占后台槽，多账户领取不与outcome反序锁头。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import sleep
from uuid import uuid4

from sqlalchemy import event, text
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.models import SyncSchedule
from app.modules.sync_control.schedule import record_report_outcome
from tests.modules.reporting.test_jobs import _route


def enqueue(session, seed, now, *, tier, accounts, days=0, campaign="scope"):
    day = now.date() - timedelta(days=days)
    return enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier=tier,
        advertiser_ids=accounts,
        campaign_ids=[campaign],
        start=day,
        end=day,
        frozen_route=_route(seed),
        actor_id=seed.actor_id,
        due_at=now,
    )


def test_full_live_window_leaves_background_capacity_available(
    session, sync_seed, redis_client
):
    from app.modules.reporting import job_tasks  # noqa: F401

    now = datetime.now(UTC)
    for index in range(36):
        identity = enqueue(
            session,
            sync_seed,
            now,
            tier="hot",
            accounts=["acc-1"],
            campaign=f"live-{index}",
        )
        job = session.get(ReportSyncJob, identity)
        job.status, job.execution_pool = "RUNNING", "live-report"
        job.claim_token, job.claimed_until = uuid4(), now + timedelta(minutes=5)
    history_id = enqueue(
        session, sync_seed, now, tier="history", accounts=["acc-1"], days=10
    )
    session.flush()
    assert dispatch_due(session, redis_client, now=now, physical_slots=36) == 1
    session.refresh(session.get(ReportSyncJob, history_id))
    assert session.get(ReportSyncJob, history_id).execution_pool == "background"


def test_multi_scope_dispatch_does_not_deadlock_with_other_class_outcome(
    committed_sync_seed, redis_client
):
    from app.modules.reporting import job_tasks  # noqa: F401

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        realtime_id = enqueue(
            session, seed, now, tier="hot", accounts=["acc-1", "acc-2"]
        )
        history_id = enqueue(
            session, seed, now, tier="history", accounts=["acc-1", "acc-2"], days=10
        )
        heads = session.exec(
            select(SyncSchedule)
            .where(SyncSchedule.tenant_id == seed.tenant_id)
            .order_by(SyncSchedule.scope_id, SyncSchedule.work_class)
        ).all()
        history = [h for h in heads if h.work_class == "history"]
        assert len(history) == 2
        first_key = history[0].scope_id, history[0].work_class
        for head in heads:
            if head.work_class == "realtime":
                head.leased_until = now + timedelta(minutes=5)
                head.lease_token = uuid4()
                head.lease_execution_id = realtime_id
                head.lease_execution_generation = 0
        engine = session.get_bind()
    locked, enter_outcome, outcome_started = Event(), Event(), Event()

    def worker():
        with factory() as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            job = session.get(ReportSyncJob, realtime_id, with_for_update=True)
            # 精确暂停在outcome已取得排序首个head、尚未取得第二个的位置。
            session.get(SyncSchedule, first_key, with_for_update=True)
            locked.set()
            assert enter_outcome.wait(3)
            outcome_started.set()
            job.status, job.finished_at = "COMPLETE", now
            record_report_outcome(session, job)

    def after_job_lock(_connection, _cursor, sql, _params, _context, _many):
        if "FOR UPDATE OF report_sync_job" in sql:
            enter_outcome.set()
            assert outcome_started.wait(3)
            sleep(0.05)  # 让第二连接实际进入锁等待，而非仅调度到Python线程。

    event.listen(engine, "after_cursor_execute", after_job_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            assert locked.wait(3)
            with factory() as session, session.begin():
                session.execute(text("SET LOCAL statement_timeout='5s'"))
                dispatched = dispatch_due(
                    session, redis_client, now=now, physical_slots=2
                )
            future.result(timeout=6)
        assert dispatched in {0, 1}
        with factory() as session, session.begin():
            job = session.get(ReportSyncJob, history_id)
            if dispatched == 0:
                assert (
                    dispatch_due(
                        session,
                        redis_client,
                        now=now + timedelta(seconds=2),
                        physical_slots=2,
                    )
                    == 1
                )
            session.refresh(job)
            assert job.status == "RUNNING"
    finally:
        enter_outcome.set()
        event.remove(engine, "after_cursor_execute", after_job_lock)
