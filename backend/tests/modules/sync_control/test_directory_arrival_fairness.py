from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.sync_control.directory_dispatch import _candidate
from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_new_arrival_cannot_permanently_preempt_an_old_continuation(session, sync_seed):
    now = datetime.now(UTC)
    old = _directory(session, sync_seed, advertiser="acc-1")
    fresh = _directory(session, sync_seed, advertiser="acc-2")
    heads = {}
    for run in (old, fresh):
        child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.directory_run_id == run.id
            )
        ).one()
        heads[run.id] = session.get(SyncSchedule, (child.scope_id, "directory"))
    heads[old.id].last_dispatched_at = now - timedelta(days=2)
    heads[old.id].oldest_pending_at = now - timedelta(days=3)
    heads[fresh.id].last_dispatched_at = None
    heads[fresh.id].oldest_pending_at = now - timedelta(minutes=1)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "work_class": "directory",
        "now": now + timedelta(seconds=1),
        "independent_only": False,
        "discover_only": True,
    }
    assert _candidate(session, **args)[1].id == old.id
    # 老续点获得一步后让回新任务，既不饿死续点，也不永久抢占新任务。
    heads[old.id].last_dispatched_at = now
    assert _candidate(session, **args)[1].id == fresh.id


def test_periodic_deferrals_rotate_before_processing_every_copy(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.ads.directory_calibration import PERIODIC_KEY
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.directory_demand import register_directory_run
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from tests.modules.reporting.test_directory_reduction import (
        case,
        complete,
        request,
        runs,
    )

    now, _ = case(session, sync_seed, monkeypatch, active=False)
    request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}})
    complete(session, sync_seed, now - timedelta(days=2))
    previous = runs(session, sync_seed)[0]
    for _ in range(6):
        values = previous.model_dump(exclude={"id", "request_id", "request_sequence"})
        values.update(
            status="QUEUED",
            published_version=None,
            completed_at=None,
            created_at=now - timedelta(days=2),
            query=previous.query | {PERIODIC_KEY: True},
        )
        pending = AdDirectoryRun(**values)
        session.add(pending)
        session.flush()
        register_directory_run(session, pending)
    ready = _directory(session, sync_seed, advertiser="acc-2")
    child = session.exec(
        select(SyncExecutionScope).where(
            SyncExecutionScope.directory_run_id == ready.id
        )
    ).one()
    head = session.get(SyncSchedule, (child.scope_id, "directory"))
    head.last_dispatched_at = now - timedelta(minutes=1)
    # 同一头的六份不应读周期副本不能吃完本轮复核预算，遮住另一接口的实时池。
    assert (
        dispatch_directories_due(
            session,
            redis_client,
            now=now + timedelta(seconds=1),
            limit=40,
            max_claims=1,
        )
        == 1
    )
    assert head.lease_execution_id == ready.id


def test_background_denial_does_not_consume_the_oldest_candidate_turn(
    committed_sync_seed, redis_client
):
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.worker_pool import lock_background_pool

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        candidates = []
        for advertiser in ("acc-1", "acc-2"):
            run = _directory(db, seed, advertiser=advertiser)
            run.workload = "background"
            child = db.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.directory_run_id == run.id
                )
            ).one()
            candidates.append((child.scope_id, run.id))
        candidates.sort()
        old_scope, old_id = candidates[0]
        for index, (scope, _) in enumerate(candidates):
            head = db.get(SyncSchedule, (scope, "directory"))
            head.last_dispatched_at = now - timedelta(days=2) + timedelta(days=index)
    args = {"now": now + timedelta(seconds=1), "limit": 40, "max_claims": 1}
    with factory() as blocker, blocker.begin():
        assert lock_background_pool(blocker)
        with factory() as db, db.begin():
            assert dispatch_directories_due(db, redis_client, **args) == 0
    with factory() as db, db.begin():
        assert dispatch_directories_due(db, redis_client, **args) == 1
        assert (
            db.get(SyncSchedule, (old_scope, "directory")).lease_execution_id == old_id
        )
        assert db.get(AdDirectoryRun, old_id).claim_generation == 2


@pytest.mark.parametrize("independent_only", [False, True])
def test_discovery_does_not_consume_a_candidate_before_fair_selection(
    session, sync_seed, redis_client, independent_only
):
    first = _directory(session, sync_seed, advertiser="acc-1")
    second = _directory(session, sync_seed, advertiser="acc-2")
    positions = {}
    args = {
        "tenant_id": sync_seed.tenant_id,
        "work_class": "directory",
        "now": datetime.now(UTC) + timedelta(seconds=1),
        "independent_only": independent_only,
        "discover_only": True,
        "redis": redis_client,
        "scan_positions": positions,
    }
    chosen = _candidate(session, **args)[1].id
    assert chosen in {first.id, second.id}
    # 另一个工作类获选时，此候选尚无实际服务，下一次仍应获得原有机会。
    assert _candidate(session, **args)[1].id == chosen
