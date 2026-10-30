import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlmodel import delete, select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.modules.ads.directory_requests import request_directory
from app.modules.reporting import job_tasks
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from tests.modules.ads.test_directory_requests import _route
from tests.modules.reporting.directory_claiming import finish
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed
from tests.modules.sync_control.conftest import (
    isolated_sync_quota as isolated_sync_quota,
)


@pytest.fixture(autouse=True)
def directory_settings(monkeypatch, redis_client):
    del redis_client  # 先验证独立Redis库，再为该任务入口绑定同一个测试连接。
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])


def test_recovery_promotes_old_targeted_read_ahead_of_full_directory(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
    }
    assert request_directory(session, scope="directory", **args) == 10
    assert (
        request_directory(
            session,
            scope="targeted",
            refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")],
            **args,
        )
        == 3
    )
    # 模拟旧版已持久化但尚未消费的回读：新派发器应先恢复它，而非十条全量任务。
    session.exec(
        delete(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    )
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 1)
    assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 1
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    assert dispatch.task_name == "ads.targeted_step"


@pytest.mark.parametrize("foreign_tenant", [False, True])
def test_targeted_entry_rejects_full_or_foreign_runs(
    session, sync_seed, monkeypatch, redis_client, foreign_tenant
):
    from app.modules.ads import tasks
    from app.modules.ads.sync_models import AdDirectoryRun

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="targeted" if foreign_tenant else "directory",
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")]
        if foreign_tenant
        else (),
    )
    run = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).first()
    assert (
        dispatch_directories_due(
            session, redis_client, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )
        == 1
    )
    run = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.claim_generation > 1)
    ).one()
    from app.modules.sync_control.dispatch_envelope import (
        dispatch_envelope,
        enqueue_sync_dispatch,
    )

    # 有效领取/签发后仍不能把full运行交给targeted入口；foreign则在消息身份层拒绝。
    identity = enqueue_sync_dispatch(
        session,
        context=sync_seed.context,
        task_name="ads.targeted_step",
        task_key=f"test-targeted-boundary:{run.id}",
        payload={"run_id": str(run.id), "claim_generation": run.claim_generation},
        execution=run,
    )
    message = session.get(PendingDispatch, identity)
    monkeypatch.setattr(tasks, "engine", session.connection())
    monkeypatch.setattr(
        tasks,
        "run_directory_step",
        lambda **_: pytest.fail("invalid range entered platform execution"),
    )
    args = {
        "tenant_id": str(uuid4() if foreign_tenant else sync_seed.tenant_id),
        "actor_id": str(sync_seed.actor_id),
        "payload": message.payload,
        "dispatch": dispatch_envelope(message),
    }
    if foreign_tenant:
        assert tasks.targeted_step(**args) == "IGNORED"
    else:
        with pytest.raises(DomainError, match="定向回读缺少对象范围"):
            tasks.targeted_step(**args)
    # 缺失envelope不能通过任何旧入口复活原请求。
    args.pop("dispatch")
    assert tasks.targeted_step(**args) == "IGNORED"


@pytest.mark.usefixtures("redis_client")
def test_dispatch_only_refills_directory_and_respects_global_window(
    session, sync_seed, monkeypatch, redis_client
):
    """报表补位信号也须利用目录空槽，且重复唤醒不能越过在途窗口（此处固定为八个）。"""
    from app.modules.ads.sync_models import AdDirectoryRun

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_WINDOW", 8)
    from app.jobs import tasks as outbox_tasks
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    monkeypatch.setattr(outbox_tasks, "drain_dispatch", lambda: 0)
    for index in range(4):
        account = f"independent-{index}"
        session.add(
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=account,
                currency="USD",
                timezone="UTC",
            )
        )
        session.flush()
        session.add(
            BCAccountAccess(
                tenant_id=sync_seed.tenant_id,
                bc_id="bc-a",
                advertiser_id=account,
                connection_id=sync_seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=datetime.now(UTC),
            )
        )
        session.flush()
        request_directory(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            frozen_route=_route(sync_seed),
            actor_id=sync_seed.actor_id,
            advertiser_ids=[account],
            scope="targeted",
            refs=[EntityRef(sync_seed.tenant_id, account, "ad", f"changed-{index}")],
        )
    session.exec(
        delete(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    )
    session.flush()
    monkeypatch.setattr(job_tasks, "engine", session.connection())

    def unexpected_plan(**_kwargs):
        raise AssertionError("补位不能重新规划或读取外部平台")

    monkeypatch.setattr(job_tasks, "plan_due_runs", unexpected_plan)
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 8
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 0
    dispatches = session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(dispatches) == 8 and all(
        d.task_name == "ads.targeted_step" for d in dispatches
    )
    run = session.get(AdDirectoryRun, UUID(dispatches[0].payload["run_id"]))
    finish(session, redis_client, run, now=datetime.now(UTC))
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 1
