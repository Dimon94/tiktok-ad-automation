"""每个 BC 独立预算、当前身份和 Redis 互斥；一次失败不消耗另一个 BC 的预算。"""

from datetime import UTC, datetime, timedelta
from time import sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.reporting import health, planner
from app.modules.reporting.store_models import AdSyncBc, ReportSyncHealth


def _plans(seed, factory):
    with factory() as session:
        planner.ensure_bc_plans(session, now=datetime.now(UTC))
        for plan in session.exec(
            select(AdSyncBc).where(AdSyncBc.tenant_id == seed.tenant_id)
        ):
            plan.active_screened_at = datetime.now(UTC)
        session.commit()


def test_bc_scheduler_only_enqueues_once_per_minute(committed_sync_seed, monkeypatch):
    seed, factory = committed_sync_seed
    _plans(seed, factory)
    monkeypatch.setattr(health.settings, "ADS_SYNC_ENABLED", True)
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    engine = factory().bind
    assert health.schedule_health(database_engine=engine, now=now) >= 2
    health.schedule_health(database_engine=engine, now=now + timedelta(seconds=1))
    with factory() as session:
        rows = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.check_bc_health",
            )
        ).all()
        assert len(rows) == 2
        assert {row.payload["bc_id"] for row in rows} == {"bc-a", "bc-b"}
        assert all(
            row.actor_id == seed.actor_id and set(row.payload) == {"bc_id"}
            for row in rows
        )
        assert (
            session.exec(
                select(ReportSyncHealth).where(
                    ReportSyncHealth.tenant_id == seed.tenant_id
                )
            ).all()
            == []
        )


def test_failed_bc_keeps_metrics_and_next_bc_gets_full_budget(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    _plans(seed, factory)
    monkeypatch.setattr(health.settings, "ADS_SYNC_ENABLED", True)
    engine = factory().bind
    context = TenantContext(seed.tenant_id, seed.actor_id, "operator")
    old = datetime.now(UTC) - timedelta(minutes=2)
    with factory() as session:
        session.add(
            ReportSyncHealth(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                status="DEGRADED",
                metrics={"missing_directory_ads": 9},
                observed_at=old,
            )
        )
        session.commit()
    seen = []
    original = health.observe_health

    def observe(session, plan, *, now, deadline=None, measurement=None, **kwargs):
        seen.append((plan.bc_id, deadline))
        if plan.bc_id == "bc-a":
            with health.query_budget(session, seconds=0.05):
                session.execute(text("SELECT pg_sleep(1)"))
        return original(
            session, plan, now=now, deadline=deadline, measurement=measurement, **kwargs
        )

    monkeypatch.setattr(health, "observe_health", observe)
    assert (
        health.run_bc_health(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            bc_id="bc-a",
        )
        == "FAILED"
    )
    assert (
        health.run_bc_health(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            bc_id="bc-b",
        )
        == "CONTINUE"
    )
    assert seen == [("bc-a", None), ("bc-b", None)]
    with factory() as session:
        row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
        assert row.metrics == {"missing_directory_ads": 9}
        assert "MONITOR_QUERY_FAILED" in row.issues
        assert "COVERAGE_PENDING" in row.issues
        assert (
            session.get(ReportSyncHealth, (seed.tenant_id, "bc-b")).metrics[
                "coverage_status"
            ]
            == "PENDING"
        )


def test_bc_worker_lock_disabled_and_identity_checks(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    _plans(seed, factory)
    monkeypatch.setattr(health.settings, "ADS_SYNC_ENABLED", True)
    engine = factory().bind
    context = TenantContext(seed.tenant_id, seed.actor_id, "operator")
    lock = redis_client.lock(
        health._health_lock_key(seed.tenant_id, "bc-a"), timeout=30
    )
    assert lock.acquire(blocking=False)
    try:
        assert (
            health.run_bc_health(
                database_engine=engine,
                redis_client=redis_client,
                context=context,
                bc_id="bc-a",
            )
            == "BUSY"
        )
    finally:
        lock.release()
    wrong = TenantContext(seed.tenant_id, uuid4(), "operator")
    with pytest.raises(DomainError):
        health.run_bc_health(
            database_engine=engine,
            redis_client=redis_client,
            context=wrong,
            bc_id="bc-a",
        )
    outside = TenantContext(uuid4(), seed.actor_id, "operator")
    assert (
        health.run_bc_health(
            database_engine=engine,
            redis_client=redis_client,
            context=outside,
            bc_id="bc-a",
        )
        == "IGNORED"
    )
    with factory() as session:
        session.get(AdSyncBc, (seed.tenant_id, "bc-a")).enabled = False
        session.commit()
    assert (
        health.run_bc_health(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            bc_id="bc-a",
        )
        == "IGNORED"
    )
    with factory() as session:
        assert (
            session.exec(
                select(ReportSyncHealth).where(
                    ReportSyncHealth.tenant_id == seed.tenant_id
                )
            ).all()
            == []
        )


def test_crashed_worker_lock_expires_and_next_task_can_finish(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    _plans(seed, factory)
    monkeypatch.setattr(health.settings, "ADS_SYNC_ENABLED", True)
    key = health._health_lock_key(seed.tenant_id, "bc-a")
    assert redis_client.set(key, "crashed-owner", px=20, nx=True)
    sleep(0.04)
    assert (
        health.run_bc_health(
            database_engine=factory().bind,
            redis_client=redis_client,
            context=TenantContext(seed.tenant_id, seed.actor_id, "operator"),
            bc_id="bc-a",
        )
        == "CONTINUE"
    )
    assert redis_client.get(key) is None


def test_scheduler_failed_bc_does_not_block_later_bc(committed_sync_seed, monkeypatch):
    seed, factory = committed_sync_seed
    _plans(seed, factory)
    monkeypatch.setattr(health.settings, "ADS_SYNC_ENABLED", True)
    enqueue = health.enqueue_after_commit

    def enqueue_with_timeout(session, **kwargs):
        if (
            kwargs["context"].tenant_id == seed.tenant_id
            and kwargs["payload"]["bc_id"] == "bc-a"
        ):
            with health.query_budget(session, seconds=0.05):
                session.execute(text("SELECT pg_sleep(1)"))
        return enqueue(session, **kwargs)

    monkeypatch.setattr(health, "enqueue_after_commit", enqueue_with_timeout)
    health.schedule_health(database_engine=factory().bind)
    with factory() as session:
        rows = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.check_bc_health",
            )
        ).all()
        assert [row.payload["bc_id"] for row in rows] == ["bc-b"]
