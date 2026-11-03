"""后台共享池的短暂锁不能暂停独立实时读取/本地发布。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.models import SyncSchedule
from app.modules.sync_control.worker_pool import lock_background_pool
from tests.modules.reporting.test_jobs import _route
from tests.modules.sync_control.test_schedule_fencing import _directory


@pytest.mark.parametrize("kind", ["material_day", "material_links"])
def test_background_fairness_rejection_falls_through_to_live_report(
    session, sync_seed, redis_client, kind
):
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.reporting import job_tasks  # noqa: F401
    from app.modules.reporting.store_models import ReportDispatchCursor
    from app.modules.sync_control.models import SyncWorkerPool
    from app.modules.sync_control.schedule import report_endpoint
    from app.modules.sync_control.worker_pool import background_lease_count

    now = datetime.now(UTC)
    occupied = _directory(session, sync_seed, advertiser="acc-2")
    occupied.workload, occupied.status = "background", "RUNNING"
    occupied.claimed_until = now + timedelta(minutes=1)
    ready = _directory(session, sync_seed)
    ready.workload = "background"
    pool = session.get(SyncWorkerPool, "background") or SyncWorkerPool(
        pool_name="background"
    )
    pool.capacity, pool.prefetch_slots = 1, 2
    pool.fairness_state = {"directory": 100}
    session.add(pool)
    ids = []
    for day, tier in [(now.date() - timedelta(days=2), "history"), (now.date(), "hot")]:
        ids.append(
            enqueue_job(
                session,
                tenant_id=sync_seed.tenant_id,
                actor_id=sync_seed.actor_id,
                bc_id="bc-a",
                kind=kind,
                tier=tier,
                advertiser_ids=["acc-1"],
                start=day,
                end=day,
                frozen_route=_route(sync_seed),
                due_at=now,
            )
        )
    cursor = session.get(ReportDispatchCursor, sync_seed.tenant_id)
    if cursor is None:
        cursor = ReportDispatchCursor(
            tenant_id=sync_seed.tenant_id, last_dispatched_at=now
        )
        session.add(cursor)
    endpoint = report_endpoint(kind)
    cursor.fairness_state = {endpoint: {"history": 100, "realtime": -100}}
    session.flush()
    assert background_lease_count(session, now=now) == 1
    assert (
        dispatch_due(
            session,
            redis_client,
            now=now + timedelta(seconds=1),
            physical_slots=12,
            max_claims=1,
        )
        == 1
    )
    historical, live = (session.get(ReportSyncJob, identity) for identity in ids)
    assert historical.claim_token is None
    assert live.execution_pool == "live-report" and live.claim_token is not None
    assert background_lease_count(session, now=now) == 1
    assert pool.fairness_state == {"directory": 100}
    if kind == "material_day":
        # 未获得后台轮次不是无需求；原历史份额不能因实时借用被清零。
        assert cursor.fairness_state[endpoint]["history"] == 100
        pool.fairness_state = {"facts": 100, "directory": -100}
        assert (
            dispatch_due(
                session,
                redis_client,
                now=now + timedelta(seconds=2),
                physical_slots=12,
                max_claims=1,
            )
            == 1
        )
        assert historical.execution_pool == "background"
        assert historical.claim_token is not None
        assert background_lease_count(session, now=now) == 2


@pytest.mark.parametrize("family", ["live-report", "live-directory", "publisher"])
def test_background_pool_lock_does_not_block_independent_execution(
    committed_sync_seed, redis_client, family
):
    from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.ads.sync import stage_directory_page
    from app.modules.reporting import job_tasks  # noqa: F401

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        if family == "live-report":
            identity = enqueue_job(
                db,
                tenant_id=seed.tenant_id,
                actor_id=seed.actor_id,
                bc_id="bc-a",
                kind="ad_day",
                tier="hot",
                advertiser_ids=["acc-1"],
                start=now.date(),
                end=now.date(),
                frozen_route=_route(seed),
                due_at=now,
            )
        else:
            run = _directory(db, seed)
            run.workload = "live" if family == "live-directory" else "background"
            identity = run.id
            if family == "publisher":
                stage_directory_page(
                    db,
                    run_id=run.id,
                    page=DirectoryPage((), (), None, True, CallEvidence()),
                    claim_generation=run.claim_generation,
                )
    with factory() as blocker, blocker.begin():
        assert lock_background_pool(blocker)
        with factory() as db, db.begin():
            db.execute(text("SET LOCAL statement_timeout='2s'"))
            if family == "live-report":
                count = dispatch_due(
                    db, redis_client, now=now + timedelta(seconds=1), physical_slots=4
                )
                assert db.get(ReportSyncJob, identity).execution_pool == "live-report"
            else:
                count = dispatch_directories_due(
                    db, redis_client, now=now + timedelta(seconds=1), limit=4
                )
            assert count == 1
            assert db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == identity)
            ).first()


def test_background_claim_still_requires_pool_lock(committed_sync_seed, redis_client):
    from app.modules.ads import tasks  # noqa: F401

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        run = _directory(db, seed)
        run.workload = "background"
        identity = run.id
    with factory() as blocker, blocker.begin():
        assert lock_background_pool(blocker)
        with factory() as db, db.begin():
            db.execute(text("SET LOCAL statement_timeout='2s'"))
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=4,
                )
                == 0
            )
            assert not db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == identity)
            ).first()


def test_due_tenant_discovery_does_not_scan_every_account_head(session, sync_seed):
    from uuid import uuid4

    from sqlmodel import col

    from app.modules.sync_control.fairness import due_tenants
    from app.modules.sync_control.models import SyncScope
    from tests.modules.sync_control.test_balance_dispatch_scale import capture_plans

    _directory(session, sync_seed)
    scope = session.exec(
        select(SyncScope).where(SyncScope.tenant_id == sync_seed.tenant_id)
    ).one()
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.scope_id == scope.id)
    ).one()
    scopes, heads = [], []
    for i in range(12000):
        identity = uuid4()
        scopes.append(
            dict(scope.model_dump(), id=identity, identity_digest=f"{i:064x}")
        )
        heads.append(dict(head.model_dump(), scope_id=identity))
    session.execute(SyncScope.__table__.insert(), scopes)
    session.execute(SyncSchedule.__table__.insert(), heads)
    session.execute(text("ANALYZE sync_schedule"))
    found = []

    def discover():
        found.extend(
            due_tenants(
                session,
                [
                    col(SyncSchedule.active).is_(True),
                    col(SyncSchedule.work_class) == "directory",
                ],
                limit=64,
            )
        )

    plans = capture_plans(
        session,
        lambda sql: "FROM sync_schedule" in sql and "report_dispatch_cursor" in sql,
        discover,
    )
    assert found == [sync_seed.tenant_id]

    def head_rows(node):
        own = (
            node.get("Actual Rows", 0) * node.get("Actual Loops", 1)
            if node.get("Relation Name") == "sync_schedule"
            else 0
        )
        return own + sum(head_rows(child) for child in node.get("Plans", []))

    # 按真正展开的活跃头数断言；回滚测试留下的死元组会增加物理buffer命中，
    # 不应使相同的一条相关探测因用例顺序改变而随机失败。
    assert plans and max(head_rows(p["Plan"]) for p in plans) < 100, plans


def test_busy_tenant_cursor_is_skipped_without_blocking_other_dispatchers(
    committed_sync_seed, redis_client
):
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.reporting.store_models import ReportDispatchCursor

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        run = _directory(db, seed)
        run.workload = "live"
        db.add(
            ReportDispatchCursor(
                tenant_id=seed.tenant_id,
                last_dispatched_at=datetime.now(UTC),
                fairness_state={},
            )
        )
    with factory() as blocker, blocker.begin():
        blocker.get(ReportDispatchCursor, seed.tenant_id, with_for_update=True)
        with factory() as db, db.begin():
            db.execute(text("SET LOCAL statement_timeout='200ms'"))
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=4,
                )
                == 0
            )
