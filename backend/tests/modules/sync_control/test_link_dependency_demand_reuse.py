from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
)
from tests.modules.reporting.test_jobs import _route


@pytest.mark.parametrize(
    ("state", "cursor", "leased", "reusable"),
    [
        ("WAITING", False, False, True),
        ("WAITING", True, False, False),
        ("RUNNING", False, False, False),
        ("WAITING", False, True, False),
    ],
)
def test_new_link_demand_reuses_only_idle_dependency_wait(
    session, sync_seed, state, cursor, leased, reusable
):
    now = datetime.now(UTC)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "actor_id": sync_seed.actor_id,
        "bc_id": "bc-a",
        "kind": "material_links",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": now.date() - timedelta(days=2),
        "end": now.date() - timedelta(days=2),
        "frozen_route": _route(sync_seed),
    }
    identity = enqueue_job(session, requested_at=now, **args)
    job = session.get(ReportSyncJob, identity)
    job.status = state
    job.claim_generation = 3
    job.claim_token = job.claimed_until = None
    job.error_code = "material_directory_pending"
    job.due_at = now + timedelta(hours=6)
    job.progress = {
        "waiting_ids": ["VIDEO:post"],
        "dependency_checked_at": now.isoformat(),
        "dependency_signature": "old",
        "dependency_attempts": 2,
    }
    if cursor:
        job.progress = {**job.progress, "overview_next_page": 2}
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == identity)
    ).one()
    child.execution_generation = 3
    child.observed_at = now
    head = session.get(SyncSchedule, (child.scope_id, "directory"))
    head.next_eligible_at = job.due_at
    if leased:
        from uuid import uuid4

        head = session.get(SyncSchedule, (child.scope_id, "directory"))
        head.lease_execution_id = job.id
        head.lease_execution_generation = 3
        head.lease_token = uuid4()
        head.leased_until = now + timedelta(minutes=10)
    session.flush()
    new = enqueue_job(session, requested_at=now + timedelta(minutes=1), **args)
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(jobs) == (1 if reusable else 2)
    if reusable:
        assert new is None
        session.refresh(child)
        session.refresh(head)
        requirement = session.get(SyncRequirement, child.requirement_id)
        assert child.frozen_revision == requirement.desired_revision == 2
        assert child.frozen_requested_after == now + timedelta(minutes=1)
        assert job.error_code == "material_directory_pending"
        assert child.observed_at is None
        assert job.due_at == head.next_eligible_at == now + timedelta(hours=6)
    else:
        session.refresh(child)
        assert child.frozen_revision == 1 and child.observed_at == now


def test_unchanged_link_demand_keeps_delay_but_new_material_fact_wakes(
    session, sync_seed
):
    from app.modules.reporting.link_work import wake_link_dependencies
    from app.modules.reporting.material_links import (
        directory_signature,
        missing_material_link_ids,
    )
    from app.modules.sync_control.schedule import record_report_outcome
    from tests.modules.reporting.test_material_query import D, material_row, publish

    now = datetime.now(UTC)
    publish(session, sync_seed, "acc-1", [material_row("old-post", "1")])
    args = {
        "tenant_id": sync_seed.tenant_id,
        "actor_id": sync_seed.actor_id,
        "bc_id": "bc-a",
        "kind": "material_links",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": D,
        "end": D,
        "frozen_route": _route(sync_seed),
    }
    identity = enqueue_job(session, requested_at=now, **args)
    job = session.get(ReportSyncJob, identity)
    job.status, job.claim_generation = "WAITING", 1
    job.due_at = now + timedelta(hours=6)
    job.error_code = "material_directory_pending"
    job.progress = {
        "waiting_ids": sorted(
            missing_material_link_ids(
                session, tenant_id=job.tenant_id, advertiser_id="acc-1", start=D, end=D
            )
        ),
        "dependency_checked_at": now.isoformat(),
        "dependency_signature": directory_signature(session, job),
    }
    record_report_outcome(session, job)
    assert enqueue_job(session, requested_at=now + timedelta(minutes=1), **args) is None
    assert job.due_at == now + timedelta(hours=6)
    assert wake_link_dependencies(session, now=now + timedelta(minutes=2)) == 0
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("old-post", "1"), material_row("new-post", "1")],
    )
    assert wake_link_dependencies(session, now=now + timedelta(minutes=3)) == 1
    assert job.due_at == now + timedelta(minutes=3)
