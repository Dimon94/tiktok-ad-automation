"""后台四进程中，目录与补链不能共同占完历史事实的执行机会。"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_directory_direct_delivery import directory
from tests.modules.reporting.test_jobs import _route


def pending(session, seed, kind, offset, now, *, future=False):
    day = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date() - timedelta(days=offset + 2)
    return enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
        kind=kind,
        tier="history",
        advertiser_ids=["acc-1"],
        campaign_ids=[str(offset)],
        start=day,
        end=day,
        due_at=now + timedelta(minutes=5) if future else now - timedelta(minutes=1),
    )


@pytest.mark.parametrize("directory_first", [False, True])
def test_real_registered_queues_share_four_live_execution_slots(
    session, sync_seed, redis_client, directory_first
):
    from app.modules.sync_control.directory_demand import register_directory_run
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.dispatcher import dispatch_due
    from app.modules.sync_control.models import SyncSchedule

    now = datetime.now(UTC) + timedelta(seconds=1)
    for i in range(8):
        pending(session, sync_seed, "material_day", i, now)
        pending(session, sync_seed, "material_links", i, now)
        run = directory(session, sync_seed)
        run.workload = "background"
        # 独立查询scope避免把单头串行误当成共享池容量保护。
        run.query = run.query | {"page_size": 100 + i}
        register_directory_run(session, run, new_execution=True)
    dispatchers = [
        lambda: dispatch_due(session, redis_client, now=now, physical_slots=36),
        lambda: dispatch_directories_due(session, redis_client, now=now, limit=40),
    ]
    if directory_first:
        dispatchers.reverse()
    for _ in range(4):
        if sum(dispatch() for dispatch in dispatchers) == 0:
            break
    heads = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == sync_seed.tenant_id,
            SyncSchedule.leased_until > now,
            SyncSchedule.lease_execution_id.is_not(None),
        )
    ).all()
    identities = {head.lease_execution_id for head in heads}
    assert len(identities) <= 4
    assert identities
    for message in session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    ).all():
        if "job_id" in message.payload:
            assert message.task_name == "reporting.backfill_step"
    before = identities
    for dispatch in dispatchers:
        dispatch()
    heads = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == sync_seed.tenant_id,
            SyncSchedule.leased_until > now,
            SyncSchedule.lease_execution_id.is_not(None),
        )
    ).all()
    assert {head.lease_execution_id for head in heads} == before


def pool_backlog(session, seed, now, *, facts=True, future=False):
    from app.modules.sync_control.directory_demand import register_directory_run

    for i in range(8):
        if facts:
            pending(session, seed, "material_day", i, now, future=future)
        pending(session, seed, "material_links", i, now)
        run = directory(session, seed)
        run.workload = "background"
        run.query = run.query | {"page_size": 100 + i}
        register_directory_run(session, run, new_execution=True)
    session.flush()


def dispatch_pool(session, redis, now, *, directory_first=False, allow_backfill=True):
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.dispatcher import dispatch_due

    actions = [
        lambda: dispatch_due(
            session, redis, now=now, physical_slots=36, allow_backfill=allow_backfill
        ),
        lambda: dispatch_directories_due(
            session, redis, now=now, limit=40, allow_backfill=allow_backfill
        ),
    ]
    if directory_first:
        actions.reverse()
    total = 0
    for _ in range(4):
        claimed = sum(action() for action in actions)
        total += claimed
        if not claimed:
            break
    return total


def pool_running(session, seed, now):
    from app.modules.sync_control.models import SyncSchedule

    identities = set(
        session.exec(
            select(SyncSchedule.lease_execution_id).where(
                SyncSchedule.tenant_id == seed.tenant_id,
                SyncSchedule.leased_until > now,
                SyncSchedule.lease_execution_id.is_not(None),
            )
        ).all()
    )
    result = []
    for identity in identities:
        run = session.get(ReportSyncJob, identity)
        if run is not None:
            result.append((run.kind, run))
        else:
            run = session.get(AdDirectoryRun, identity)
            if run is not None and run.workload == "background":
                result.append(("directory", run))
    return result


def finish_pool_step(session, redis, kind, run, now):
    from uuid import uuid4

    from app.core.config import settings
    from app.modules.ads.directory_dispatch import directory_step_key
    from app.modules.sync_control.costs import record_cost_receipt
    from app.modules.sync_control.directory_demand import (
        directory_endpoint,
        directory_outcome,
    )
    from app.modules.sync_control.models import SyncCostLedger
    from app.modules.sync_control.occupancy import release_dispatch
    from app.modules.sync_control.schedule import record_report_outcome, report_endpoint

    # 调度测试模拟运输步骤完成；真实SDK成本另由directory_continuation覆盖。
    if kind == "directory":
        identity, endpoint = directory_step_key(run), directory_endpoint(run)
        run.status = "COMPLETE"
        run.completed_at = now
        directory_outcome(session, run)
        cost_filter = SyncCostLedger.directory_run_id == run.id
        advertiser = run.advertiser_id
    else:
        identity, endpoint = run.claim_token, report_endpoint(run.kind)
        run.status = "COMPLETE"
        run.finished_at = now
        record_report_outcome(session, run)
        cost_filter = SyncCostLedger.report_job_id == run.id
        advertiser = run.advertiser_ids[0]
    release_dispatch(
        redis,
        reservation_id=identity,
        app_scope=settings.TIKTOK_APP_ID,
        endpoint=endpoint,
        rate_group=endpoint,
        tenant_id=run.tenant_id,
        advertiser_id=advertiser,
    )
    for cost in session.exec(
        select(SyncCostLedger).where(
            cost_filter, SyncCostLedger.generation == run.claim_generation
        )
    ).all():
        record_cost_receipt(session, cost.id, uuid4(), total_units=1, closed=True)
    run.claim_token = run.claimed_until = None
    session.flush()


@pytest.mark.parametrize("directory_first", [False, True])
def test_shared_pool_gives_every_backlogged_kind_progress(
    session, sync_seed, redis_client, directory_first
):
    now = datetime.now(UTC) + timedelta(seconds=1)
    pool_backlog(session, sync_seed, now)
    initial_reports = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    initial_directories = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    kinds = set()
    for _ in range(6):
        # outcome使用真实DB/墙钟写下一到期时点；每轮同步推进测试时钟，
        # 不因真实探测超过初始1秒而把已可运行工作误判为未来任务。
        now = datetime.now(UTC) + timedelta(seconds=1)
        dispatch_pool(session, redis_client, now, directory_first=directory_first)
        active = pool_running(session, sync_seed, now)
        if not active:
            # 新需求合并可能提前耗尽夹具；仅允许所有原请求均终结后结束轮转。
            assert all(r.status == "COMPLETE" for r in initial_reports), [
                (r.kind, r.status, str(r.due_at), r.error_code)
                for r in initial_reports
                if r.status != "COMPLETE"
            ]
            assert all(r.status == "COMPLETE" for r in initial_directories)
            break
        assert 1 <= len(active) <= 4
        kinds.update(kind for kind, _ in active)
        for kind, run in active:
            finish_pool_step(session, redis_client, kind, run, now)
    assert kinds == {"material_day", "material_links", "directory"}


@pytest.mark.parametrize("future", [False, True])
def test_idle_or_future_fact_share_can_be_borrowed(
    session, sync_seed, redis_client, future
):
    now = datetime.now(UTC) + timedelta(seconds=1)
    pool_backlog(session, sync_seed, now, facts=future, future=future)
    dispatch_pool(session, redis_client, now)
    active = pool_running(session, sync_seed, now)
    assert len(active) == 4
    assert {kind for kind, _ in active} <= {"material_links", "directory"}


def test_arriving_facts_reclaim_next_released_slot_without_cancelling_work(
    session, sync_seed, redis_client
):
    now = datetime.now(UTC) + timedelta(seconds=1)
    pool_backlog(session, sync_seed, now, facts=False)
    dispatch_pool(session, redis_client, now, directory_first=True)
    active = pool_running(session, sync_seed, now)
    assert len(active) == 4
    pending(session, sync_seed, "material_day", 50, now)
    held = {run.id for _, run in active[1:]}
    finish_pool_step(session, redis_client, *active[0], now)
    dispatch_pool(session, redis_client, now, directory_first=True)
    current = pool_running(session, sync_seed, now)
    assert len(current) == 4
    assert held <= {run.id for _, run in current}
    assert any(kind == "material_day" for kind, _ in current)


@pytest.mark.parametrize("service", [None, "status"])
def test_realtime_directory_does_not_take_shared_background_slot(
    session, sync_seed, redis_client, service
):
    from app.modules.sync_control.directory_demand import register_directory_run

    now = datetime.now(UTC) + timedelta(seconds=1)
    pool_backlog(session, sync_seed, now)
    dispatch_pool(session, redis_client, now)
    assert len(pool_running(session, sync_seed, now)) == 4
    live = directory(session, sync_seed)
    live.query = live.query | {"page_size": 200}
    if service is not None:
        live.query = live.query | {"directory_service_v1": service}
    register_directory_run(session, live, new_execution=True)
    assert dispatch_pool(session, redis_client, now) >= 1
    assert live.claim_generation > 1
    assert len(pool_running(session, sync_seed, now)) == 4


def test_simultaneous_dispatchers_never_overfill_shared_pool(
    committed_sync_seed, redis_client
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    seed, factory = committed_sync_seed
    now = datetime.now(UTC) + timedelta(seconds=1)
    with factory() as db, db.begin():
        pool_backlog(db, seed, now)
    barrier = Barrier(4)

    def claim(index):
        barrier.wait(timeout=10)
        with factory() as db, db.begin():
            return dispatch_pool(db, redis_client, now, directory_first=bool(index % 2))

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(claim, range(4)))
    with factory() as db:
        active = pool_running(db, seed, now)
        assert 1 <= len(active) <= 4
        assert len({run.id for _, run in active}) == len(active)


def test_paused_history_budget_does_not_reserve_idle_pool_slots(
    session, sync_seed, redis_client
):
    now = datetime.now(UTC) + timedelta(seconds=1)
    pool_backlog(session, sync_seed, now)
    dispatch_pool(session, redis_client, now, allow_backfill=False)
    active = pool_running(session, sync_seed, now)
    assert len(active) == 4 and {kind for kind, _ in active} == {"directory"}


def directory_only_backlog(session, seed):
    from app.modules.sync_control.directory_demand import register_directory_run

    for i in range(8):
        run = directory(session, seed)
        run.workload = "background"
        run.query = run.query | {"page_size": 100 + i}
        register_directory_run(session, run, new_execution=True)
    session.flush()


@pytest.mark.parametrize(
    "state", ["queued", "worker", "expired", "old_generation", "old_owner", "publisher"]
)
def test_shared_pool_counts_only_current_remote_execution_leases(
    session, sync_seed, redis_client, state
):
    from app.modules.ads.sync_models import AdDirectoryPage
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.models import SyncSchedule, SyncScope

    now = datetime.now(UTC) + timedelta(seconds=1)
    directory_only_backlog(session, sync_seed)
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == 4
    heads = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == sync_seed.tenant_id,
            SyncSchedule.lease_token.is_not(None),
        )
    ).all()
    assert len(heads) == 4
    for head in heads:
        run = session.get(AdDirectoryRun, head.lease_execution_id)
        if state == "worker":
            run.claimed_until = now + timedelta(minutes=1)
            head.leased_until = now - timedelta(seconds=1)
        elif state == "expired":
            run.claimed_until = now - timedelta(seconds=1)
        elif state == "old_generation":
            run.claim_generation += 1
        elif state == "old_owner":
            session.get(SyncScope, head.scope_id).owner_epoch += 1
        elif state == "publisher":
            session.add(
                AdDirectoryPage(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    advertiser_id=run.advertiser_id,
                    page=1,
                    complete=True,
                    claim_generation=run.claim_generation,
                    items=[],
                    materials=[],
                )
            )
    session.flush()
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == (
        0 if state in {"queued", "worker"} else 4
    )


def test_multiple_heads_for_one_execution_use_one_pool_slot(
    session, sync_seed, redis_client
):
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.models import SyncSchedule

    now = datetime.now(UTC) + timedelta(seconds=1)
    directory_only_backlog(session, sync_seed)
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == 4
    heads = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == sync_seed.tenant_id,
            SyncSchedule.lease_token.is_not(None),
        )
    ).all()
    for head in heads:
        session.add(SyncSchedule(**(head.model_dump() | {"work_class": "realtime"})))
    session.flush()
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == 0
    run = session.get(AdDirectoryRun, heads[0].lease_execution_id)
    finish_pool_step(session, redis_client, "directory", run, now)
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == 1
