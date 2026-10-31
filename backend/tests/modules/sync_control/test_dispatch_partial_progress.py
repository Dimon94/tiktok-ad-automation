"""后续派发超时不能撤销已经领取的工作，也不能遗留Redis排队占位。"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.db import engine
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.query_budget import QueryBudgetExceeded
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.occupancy import occupancy_keys, reserve_dispatch
from tests.modules.reporting.test_jobs import D, _route


@pytest.mark.parametrize("failure", ["budget", "statement_timeout"])
def test_directory_timeout_keeps_report_dispatch_and_refunds_uncommitted_reservation(
    committed_sync_seed, redis_client, monkeypatch, failure
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        job_id = enqueue_job(
            db,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            due_at=now,
        )
    scope = {
        "app_scope": job_tasks.settings.TIKTOK_APP_ID,
        "endpoint": "/campaign/get/",
        "rate_group": "/campaign/get/",
        "tenant_id": seed.tenant_id,
        "advertiser_id": "acc-2",
    }

    def fail_directory(session, *_args, **_kwargs):
        ready = reserve_dispatch(
            redis_client,
            reservation_id="failed-directory",
            policy=admission_policy(
                "reports.integrated",
                channel="OFFICIAL_API",
                physical_endpoint=scope["endpoint"],
            ),
            **scope,
        )
        assert ready.capacity_hint > 0
        if failure == "statement_timeout":
            session.connection().exec_driver_sql("SET LOCAL statement_timeout='10ms'")
            session.execute(text("SELECT pg_sleep(0.05)"))
        raise QueryBudgetExceeded()

    monkeypatch.setattr(job_tasks, "_dispatch_directories", fail_directory)
    assert (
        job_tasks._dispatch_pending(
            engine, now + timedelta(seconds=1), budget_seconds=4
        )
        == 1
    )
    with factory() as db:
        assert db.get(ReportSyncJob, job_id).status == "RUNNING"
        assert db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).first()
    assert all(
        redis_client.zscore(key, "failed-directory") is None
        for key in occupancy_keys(**scope)
    )


def test_directory_commit_batch_does_not_reduce_total_inflight_window(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.ads import tasks  # noqa:F401
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from tests.modules.sync_control.test_schedule_fencing import _directory

    monkeypatch.setattr(job_tasks.settings, "TIKTOK_APP_ID", str(sync_seed.tenant_id))
    _directory(session, sync_seed)
    _directory(session, sync_seed, advertiser="acc-2")
    now = datetime.now(UTC) + timedelta(seconds=1)
    # 一次提交一条，下一次仍可填充第二个空槽，不能把总窗口误改为1。
    for _ in range(2):
        assert (
            dispatch_directories_due(
                session, redis_client, now=now, limit=40, max_claims=1
            )
            == 1
        )
    assert (
        dispatch_directories_due(session, redis_client, now=now, limit=40, max_claims=1)
        == 0
    )
