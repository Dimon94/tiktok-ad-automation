"""周期定向刷新只唤醒公平派发；新一轮不能绕过已经排队的账户。"""

from datetime import UTC, datetime, timedelta

from sqlmodel import delete, select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.directory_refresh import refresh_report_directory
from tests.modules.reporting.test_directory_refresh import prepared
from tests.modules.reporting.test_jobs import _route


def dispatches(session, seed):
    return session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
    ).all()


def test_new_periodic_refresh_wakes_dispatch_without_overtaking_backlog(
    session, sync_seed, monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    import os

    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 1)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        frozen_route=_route(sync_seed),
        advertiser_ids=["acc-1"],
        scope="targeted",
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "old-ad")],
        ad_types={"acc-1": {"SMART_PLUS": ("ad",)}},
    )
    old = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).one()
    old.created_at = datetime.now(UTC) - timedelta(hours=1)
    # 模拟窗口此前全满留下的持久积压，现在有一个空槽。
    session.exec(
        delete(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    )
    job = prepared(session, sync_seed)

    assert refresh_report_directory(session, job) == 3
    pending = dispatches(session, sync_seed)
    assert len(pending) == 1
    assert pending[0].task_name == "reporting.plan_due"
    assert pending[0].payload == {"dispatch_only": True}
    assert pending[0].published_at is None
    assert pending[0].available_at <= datetime.now(UTC)
    assert refresh_report_directory(session, job) == 0
    assert len(dispatches(session, sync_seed)) == 1

    from uuid import UUID

    from app.modules.ads.directory_dispatch import directory_step_key
    from app.modules.sync_control.directory_demand import (
        directory_endpoint,
        directory_outcome,
    )
    from app.modules.sync_control.occupancy import release_dispatch

    expected = {
        run.id
        for run in session.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == sync_seed.tenant_id
            )
        ).all()
    }
    served = []
    for turn in range(len(expected)):
        assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 1
        targeted = [
            row
            for row in dispatches(session, sync_seed)
            if row.task_name == "ads.targeted_step"
        ]
        fresh = [row for row in targeted if UUID(row.payload["run_id"]) not in served]
        assert len(fresh) == 1 and len(targeted) == turn + 1
        current = session.get(AdDirectoryRun, UUID(fresh[0].payload["run_id"]))
        if turn == 0:
            assert (
                current.id == old.id
            )  # 同实时工作类按持久due轮转，不让新批覆盖旧积压。
        served.append(current.id)
        assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 0
        assert (
            len(dispatches(session, sync_seed)) == len(targeted) + 1
        )  # 原纯唤醒仍一条。
        # 模拟有界消费者已完成；仅释放该真实queued身份，不改平台已发送计数。
        endpoint = directory_endpoint(current)
        release_dispatch(
            redis_client,
            reservation_id=directory_step_key(current),
            app_scope=settings.TIKTOK_APP_ID,
            endpoint=endpoint,
            rate_group=endpoint,
            tenant_id=current.tenant_id,
            advertiser_id=current.advertiser_id,
        )
        current.status = "COMPLETE"
        directory_outcome(session, current)
        session.flush()
    assert set(served) == expected and len(served) == len(expected)
    assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 0
