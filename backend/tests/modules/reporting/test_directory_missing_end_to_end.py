"""回归：新缺口不能因同租户旧状态刷新而完全失去首轮执行机会。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import refresh_report_directory
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement
from tests.modules.reporting.directory_claiming import finish, pending, sent
from tests.modules.reporting.test_directory_refresh import prepared
from tests.modules.sync_control.conftest import (
    isolated_sync_quota as isolated_sync_quota,
)


@pytest.mark.parametrize("old_service", ["identity", "status"])
def test_missing_demand_gets_service_with_old_backlog(
    session, sync_seed, monkeypatch, redis_client, old_service
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    old_ids = set()
    for _ in range(40):
        run = pending(session, sync_seed, old_service, age=60)
        old_ids.add(run.id)
        session.add(
            AdObject(
                tenant_id=run.tenant_id,
                advertiser_id=run.advertiser_id,
                kind=run.kind,
                remote_id=run.query["ids"][0],
                ad_type=run.ad_type,
                observed_at=now - timedelta(hours=2),
                published_version=1,
            )
        )
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    new_ids = {r.id for r in session.exec(select(AdDirectoryRun)).all()} - old_ids
    assert len(new_ids) == 3
    # 新合同每canonical头只运行一条，窗口按实际执行而非旧32条请求计数。
    assert (
        dispatch_directories_due(
            session, redis_client, now=datetime.now(UTC) + timedelta(seconds=1), limit=4
        )
        >= 1
    )
    delivered_ids = {r.id for r in sent(session)}
    assert delivered_ids & new_ids, "独立缺口scope必须获得执行机会"
    untouched = new_ids - delivered_ids
    for child in session.exec(
        select(SyncExecutionScope).where(
            SyncExecutionScope.directory_run_id.in_(untouched)
        )
    ).all():
        requirement = session.get(SyncRequirement, child.requirement_id)
        assert requirement.satisfied_revision < requirement.desired_revision
    # 有限旧范围依次完成后，同scope后继需求仍被唤醒；不靠改查询/重置旧页抢占。
    completed = set()
    for _ in range(len(old_ids) + len(new_ids) + 1):
        for run in sent(session):
            if run.id not in completed:
                finish(session, redis_client, run, now=datetime.now(UTC))
                completed.add(run.id)
        if new_ids <= completed:
            break
        assert (
            dispatch_directories_due(
                session,
                redis_client,
                now=datetime.now(UTC) + timedelta(seconds=1),
                limit=4,
            )
            >= 1
        )
    assert new_ids <= completed
    for child in session.exec(
        select(SyncExecutionScope).where(
            SyncExecutionScope.directory_run_id.in_(new_ids)
        )
    ).all():
        requirement = session.get(SyncRequirement, child.requirement_id)
        assert requirement.satisfied_revision == requirement.desired_revision
