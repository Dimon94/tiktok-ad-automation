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
    from app.modules.sync_control.fairness import lock_tenant_cursor

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        run = _directory(db, seed)
        run.workload = "live"
        assert lock_tenant_cursor(db, seed.tenant_id) is not None
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
