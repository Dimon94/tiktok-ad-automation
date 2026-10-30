from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.contracts import ExecutionFence
from app.modules.sync_control.directory_demand import (
    directory_outcome,
    register_directory_run,
)
from app.modules.sync_control.models import SyncRequirement, SyncSchedule, SyncScope
from app.modules.sync_control.requirements import satisfy_requirements
from tests.modules.reporting.test_jobs import _route


def _directory(
    session, seed, advertiser="acc-1", query_extra=None, new_execution=False
):
    run = AdDirectoryRun(
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        advertiser_id=advertiser,
        bc_id="bc-a",
        connection_id=seed.connection.id,
        channel="OFFICIAL_API",
        frozen_route=_route(seed),
        partition_key=uuid4().hex * 2,
        query={
            "kind": "ad",
            "ad_type": "SMART_PLUS",
            "ids": [uuid4().hex],
            **(query_extra or {}),
        },
        kind="ad",
        ad_type="SMART_PLUS",
        claim_generation=1,
    )
    session.add(run)
    session.flush()
    register_directory_run(
        session, run, **({"new_execution": True} if new_execution else {})
    )
    return run


def test_finished_partition_preserves_other_partition_and_newer_head_lease(
    session, sync_seed
):
    first, second = _directory(session, sync_seed), _directory(session, sync_seed)
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    token = uuid4()
    due = datetime.now(UTC) + timedelta(minutes=1)
    head.lease_token, head.leased_until = token, due
    head.lease_execution_id, head.lease_execution_generation = (
        second.id,
        second.claim_generation,
    )
    first.status = "COMPLETE"
    satisfy_requirements(
        session,
        fence=ExecutionFence(first.id, "directory", 1, 1),
        observed_at=datetime.now(UTC),
        publication_version=1,
    )
    directory_outcome(session, first)
    assert head.active and head.lease_token == token and head.leased_until == due
    pending = session.exec(
        select(SyncRequirement).where(
            SyncRequirement.tenant_id == sync_seed.tenant_id,
            SyncRequirement.satisfied_revision < SyncRequirement.desired_revision,
        )
    ).all()
    assert len(pending) == 1


def test_old_execution_generation_cannot_satisfy_bucket(session, sync_seed):
    run = _directory(session, sync_seed)
    assert (
        satisfy_requirements(
            session,
            fence=ExecutionFence(run.id, "directory", 0, 1),
            observed_at=datetime.now(UTC),
            publication_version=1,
        )
        == []
    )
    requirement = session.exec(
        select(SyncRequirement).where(SyncRequirement.tenant_id == sync_seed.tenant_id)
    ).one()
    assert requirement.satisfied_revision == 0


def test_periodic_and_status_metadata_do_not_duplicate_directory_scope(
    session, sync_seed
):
    _directory(session, sync_seed)
    _directory(
        session,
        sync_seed,
        query_extra={"periodic_directory_v1": True, "directory_service_v1": "status"},
    )
    scopes = session.exec(
        select(SyncScope).where(SyncScope.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(scopes) == 1


def test_new_directory_execution_inherits_owner_without_upgrading_old_run(
    session, sync_seed
):
    import pytest

    from app.core.errors import DomainError
    from app.modules.sync_control.directory_demand import directory_fence
    from app.modules.sync_control.models import SyncScope

    first = _directory(session, sync_seed)
    scope = session.exec(
        select(SyncScope).where(SyncScope.tenant_id == sync_seed.tenant_id)
    ).one()
    scope.owner_epoch = 2
    session.flush()
    second = _directory(session, sync_seed, new_execution=True)
    assert second.owner_epoch == 2 and first.owner_epoch == 1
    with pytest.raises(DomainError, match="所有权"):
        directory_fence(session, first, claim=True)


def test_directory_backlog_cannot_take_second_tenant_turn(
    session, sync_seed, redis_client, monkeypatch
):
    from app.core.config import settings
    from app.integrations.tiktok.platform_quotas import quota_configuration
    from app.jobs.models import PendingDispatch
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"fairness-{uuid4()}")
    first = _directory(session, sync_seed)
    _directory(session, sync_seed, advertiser="acc-2")
    other = seed_fixture.__wrapped__(session)
    second = _directory(session, other)
    # A 的两个头都更老；单按头排序会连续服务 A，租户游标应给 B 第二轮。
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=2) == 2
    messages = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id.in_([first.tenant_id, second.tenant_id]),
            PendingDispatch.task_name.in_(["ads.sync_step", "ads.targeted_step"]),
        )
    ).all()
    assert {row.tenant_id for row in messages} == {first.tenant_id, second.tenant_id}


def test_same_scope_identity_backlog_does_not_hide_current_status(
    session, sync_seed, redis_client, monkeypatch
):
    from app.core.config import settings
    from app.integrations.tiktok.platform_quotas import quota_configuration
    from app.jobs.models import PendingDispatch
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"status-turn-{uuid4()}")
    _directory(session, sync_seed, query_extra={"directory_service_v1": "identity"})
    status = _directory(
        session, sync_seed, query_extra={"directory_service_v1": "status_missing"}
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    message = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == "ads.targeted_step",
        )
    ).one()
    assert message.payload["run_id"] == str(status.id)


def test_existing_unstarted_directory_promotes_its_head_when_status_is_requested(
    session, sync_seed
):
    run = _directory(
        session, sync_seed, query_extra={"directory_service_v1": "identity"}
    )
    from app.modules.ads.directory_services import promote_status_requests

    assert promote_status_requests(session, [run], missing_run_ids={run.id}) == 1
    requirement = session.exec(
        select(SyncRequirement).where(SyncRequirement.tenant_id == sync_seed.tenant_id)
    ).one()
    assert requirement.source == "realtime"
    heads = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == sync_seed.tenant_id, SyncSchedule.active.is_(True)
        )
    ).all()
    assert [head.work_class for head in heads] == ["realtime"]


def test_terminal_failure_retires_execution_but_keeps_unmet_requirement_recoverable(
    session, sync_seed
):
    from datetime import date

    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from app.modules.sync_control.models import SyncExecutionScope
    from app.modules.sync_control.schedule import record_report_outcome

    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": date(2026, 10, 1),
        "end": date(2026, 10, 1),
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    job_id = enqueue_job(session, **args)
    job = session.get(ReportSyncJob, job_id)
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    head.lease_execution_id, head.lease_execution_generation = (
        job.id,
        job.claim_generation,
    )
    job.status, job.error_code = "FAILED", "provider_timeout"
    record_report_outcome(session, job)
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job.id)
    ).one()
    requirement = session.get(SyncRequirement, child.requirement_id)
    assert child.status == "SUPERSEDED"
    assert requirement.satisfied_revision == 0 and requirement.desired_revision == 1
    assert not head.active and head.wait_reason == "TERMINAL:provider_timeout"
    successor = enqueue_job(session, **args)
    session.refresh(head)
    assert successor is not None and head.active


def test_directory_request_after_owner_change_creates_new_execution_in_same_slot(
    session, sync_seed, monkeypatch
):
    from app.core.config import settings
    from app.modules.ads.directory_requests import request_directory
    from app.modules.sync_control.models import SyncExecutionScope

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
        "scope": "directory",
        "ad_types": {"acc-1": {"SMART_PLUS": ["ad"]}},
        "now": datetime.now(UTC),
    }
    assert request_directory(session, **args) == 1
    old = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).one()
    scope = session.exec(
        select(SyncScope).where(SyncScope.tenant_id == sync_seed.tenant_id)
    ).one()
    scope.owner_epoch += 1
    session.flush()
    assert request_directory(session, **args) == 1
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(runs) == 2 and old.owner_epoch == 1
    new = next(run for run in runs if run.id != old.id)
    assert new.owner_epoch == 2 and new.request_id != old.request_id
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.directory_run_id == new.id)
    ).one()
    assert child.owner_epoch == 2
    assert request_directory(session, **args) == 0


def test_periodic_filter_does_not_hide_a_previous_owner_execution(
    session, sync_seed, monkeypatch
):
    from app.core.config import settings
    from app.modules.ads.directory_requests import (
        directory_unblocked_accounts,
        request_directory,
    )

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "advertiser_ids": ["acc-1"],
        "now": datetime.now(UTC),
    }
    assert (
        request_directory(
            session,
            **common,
            scope="directory",
            ad_types={"acc-1": {"SMART_PLUS": ["ad"]}},
        )
        == 1
    )
    assert directory_unblocked_accounts(session, **common) == []
    scope = session.exec(
        select(SyncScope).where(SyncScope.tenant_id == sync_seed.tenant_id)
    ).one()
    scope.owner_epoch += 1
    session.flush()
    assert directory_unblocked_accounts(session, **common) == ["acc-1"]


@pytest.mark.parametrize("kind", ["directory", "report"])
def test_outcome_serializes_lease_release_with_concurrent_claim(
    committed_sync_seed, monkeypatch, kind
):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    from app.modules.sync_control import schedule

    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        if kind == "directory":
            first, second = _directory(session, seed), _directory(session, seed)
            model, outcome = AdDirectoryRun, directory_outcome
        else:
            from app.modules.reporting.jobs import enqueue_job
            from app.modules.reporting.store_models import ReportSyncJob

            common = {
                "tenant_id": seed.tenant_id,
                "bc_id": "bc-a",
                "kind": "ad_day",
                "tier": "history",
                "advertiser_ids": ["acc-1"],
                "frozen_route": _route(seed),
                "actor_id": seed.actor_id,
            }
            days = [
                datetime.now(UTC).date() - timedelta(days=offset) for offset in (10, 8)
            ]
            first, second = [
                session.get(
                    ReportSyncJob, enqueue_job(session, **common, start=day, end=day)
                )
                for day in days
            ]
            model, outcome = ReportSyncJob, schedule.record_report_outcome
        identities = first.id, second.id
        head = session.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        head_key = head.scope_id, head.work_class
        head.lease_execution_id, head.lease_execution_generation = (
            first.id,
            first.claim_generation,
        )
        head.lease_token, head.leased_until = (
            uuid4(),
            datetime.now(UTC) - timedelta(seconds=1),
        )
    new_token = uuid4()

    def claim():
        # 独立数据库会话模仿领取事务持有head行锁；验证的是读后清租约的交错，
        # 具体候选/平台准入另由真实dispatcher用例覆盖。
        with factory() as other, other.begin():
            head = other.get(SyncSchedule, head_key, with_for_update=True)
            head.lease_execution_id, head.lease_execution_generation = identities[1], 1
            head.lease_token, head.leased_until = (
                new_token,
                datetime.now(UTC) + timedelta(minutes=2),
            )

    original = schedule.pending_execution_dues
    futures = []
    with ThreadPoolExecutor(max_workers=1) as pool:

        def interleaved(*args, **kwargs):
            futures.append(pool.submit(claim))
            try:
                futures[-1].result(timeout=0.2)
            except TimeoutError:
                pass  # 正确锁序必须让新领取等本次outcome提交。
            return original(*args, **kwargs)

        monkeypatch.setattr(schedule, "pending_execution_dues", interleaved)
        with factory() as session, session.begin():
            first = session.get(model, identities[0])
            first.status, first.error_code = "FAILED", "provider_timeout"
            outcome(session, first)
        futures[-1].result(timeout=3)
    with factory() as session:
        head = session.get(SyncSchedule, head_key)
        assert (
            head.lease_execution_id == identities[1] and head.lease_token == new_token
        )
