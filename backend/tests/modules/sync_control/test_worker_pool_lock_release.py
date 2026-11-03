"""拒绝未分配工作的尝试必须立即释放自己新取的公共池锁。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.modules.reporting.jobs import enqueue_job
from app.modules.sync_control.models import SyncWorkerPool
from app.modules.sync_control.worker_pool import reserve_background_turn
from tests.modules.reporting.test_jobs import _route
from tests.modules.sync_control.test_schedule_fencing import _directory


@pytest.mark.parametrize("reason", ["capacity", "fairness"])
def test_denied_attempt_releases_pool_before_outer_transaction_finishes(
    committed_sync_seed, redis_client, reason
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        pool = db.get(SyncWorkerPool, "background") or SyncWorkerPool(
            pool_name="background"
        )
        before = (pool.capacity, pool.prefetch_slots, dict(pool.fairness_state))
        pool.capacity, pool.prefetch_slots = 1, 0 if reason == "capacity" else 2
        pool.fairness_state = {"facts": 100, "directory": -100}
        db.add(pool)
        run = _directory(db, seed)
        run.workload, run.status = "background", "RUNNING"
        run.claimed_until = now + timedelta(minutes=1)
        enqueue_job(
            db,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=now.date() - timedelta(days=2),
            end=now.date() - timedelta(days=2),
            frozen_route=_route(seed),
            due_at=now,
        )
    try:
        with factory() as first, first.begin():
            assert not reserve_background_turn(
                first, redis_client, category="directory", now=now
            )
            if reason == "fairness":
                from app.core.config import settings

                key = f"sync:background-readiness:v2:{settings.TIKTOK_APP_ID}"
                assert redis_client.hget(key, "facts") is not None
            # 第一事务继续独立工作，第二事务仍应能取得共享池和池行。
            with factory() as second, second.begin():
                assert second.execute(
                    text(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:worker-pool:background',0))"
                    )
                ).scalar_one()
                assert (
                    second.execute(
                        text(
                            "SELECT pool_name FROM sync_worker_pool WHERE pool_name='background' FOR UPDATE NOWAIT"
                        )
                    ).scalar_one()
                    == "background"
                )
    finally:
        with factory() as db, db.begin():
            pool = db.get(SyncWorkerPool, "background")
            pool.capacity, pool.prefetch_slots, pool.fairness_state = before


def test_successful_allocation_keeps_pool_lock_until_claim_transaction_commits(
    committed_sync_seed, redis_client
):
    _, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as first, first.begin():
        assert reserve_background_turn(
            first, redis_client, category="directory", now=now
        )
        with factory() as second, second.begin():
            assert not second.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:worker-pool:background',0))"
                )
            ).scalar_one()


def test_rejected_followup_does_not_release_prior_successful_allocation(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        pool = db.get(SyncWorkerPool, "background") or SyncWorkerPool(
            pool_name="background"
        )
        db.add(pool)
        before = (pool.capacity, pool.prefetch_slots)
        pool.capacity, pool.prefetch_slots = 1, 0
    try:
        with factory() as first, first.begin():
            assert reserve_background_turn(
                first, redis_client, category="directory", now=now
            )
            run = _directory(first, seed)
            run.workload, run.status = "background", "RUNNING"
            run.claimed_until = now + timedelta(minutes=1)
            assert not reserve_background_turn(
                first, redis_client, category="directory", now=now
            )
            # 后一次失败不能释放外层已成功分配的锁，未提交的真实占位不可被越过。
            with factory() as second, second.begin():
                assert not second.execute(
                    text(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:worker-pool:background',0))"
                    )
                ).scalar_one()
    finally:
        with factory() as db, db.begin():
            pool = db.get(SyncWorkerPool, "background")
            pool.capacity, pool.prefetch_slots = before
