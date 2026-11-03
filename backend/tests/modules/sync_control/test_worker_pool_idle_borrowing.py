from datetime import UTC, datetime, timedelta

import pytest

from app.modules.reporting.jobs import enqueue_job
from app.modules.sync_control.models import SyncWorkerPool
from app.modules.sync_control.worker_pool import (
    background_lease_count,
    reserve_background_turn,
)
from tests.modules.reporting.test_jobs import _route
from tests.modules.sync_control.test_schedule_fencing import _directory


@pytest.mark.parametrize(("occupied", "expected"), [(0, True), (1, False), (3, False)])
def test_idle_worker_borrows_turn_but_busy_pool_keeps_fairness(
    session, sync_seed, redis_client, occupied, expected
):
    now = datetime.now(UTC)
    pool = session.get(SyncWorkerPool, "background") or SyncWorkerPool(
        pool_name="background"
    )
    pool.capacity, pool.prefetch_slots = 1, 2
    pool.fairness_state = {"facts": 100, "directory": -100}
    session.add(pool)
    for _ in range(occupied):
        run = _directory(session, sync_seed)
        run.workload = "background"
        run.status = "RUNNING"
        run.claimed_until = now + timedelta(minutes=1)
    enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        actor_id=sync_seed.actor_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="history",
        advertiser_ids=["acc-1"],
        start=now.date() - timedelta(days=2),
        end=now.date() - timedelta(days=2),
        frozen_route=_route(sync_seed),
        due_at=now,
    )
    session.flush()
    assert background_lease_count(session, now=now) == occupied
    assert (
        reserve_background_turn(session, redis_client, category="directory", now=now)
        is expected
    )
    if expected:
        assert pool.fairness_state["directory"] < -100
        assert pool.fairness_state["facts"] > 100
