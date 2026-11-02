from datetime import UTC, date, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.contracts import ExecutionFence
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncScope,
)
from app.modules.sync_control.report_demand import adopt_report_job
from app.modules.sync_control.requirements import satisfy_requirements
from tests.modules.reporting.test_jobs import _route


def compact(session, job_id, now):
    path = (
        Path(__file__).resolve().parents[4] / "scripts/compact-covered-report-runs.py"
    )
    spec = spec_from_file_location("report_compaction", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.supersede_covered_report(session, job_id=job_id, now=now)


def pair(session, seed):
    now = datetime.now(UTC)
    jobs = []
    for age in (2, 1):
        job = ReportSyncJob(
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            bc_id="bc-a",
            kind="ad_day",
            priority=3,
            advertiser_ids=["acc-1", "acc-2"],
            start_date=date(2026, 10, 5),
            end_date=date(2026, 10, 5),
            job_key=str(uuid4()),
            frozen_route=_route(seed),
            due_at=now,
            created_at=now - timedelta(hours=age),
            status="WAITING",
        )
        session.add(job)
        session.flush()
        adopt_report_job(session, job)
        jobs.append(job)
    old, newer = jobs
    satisfy_requirements(
        session,
        fence=ExecutionFence(newer.id, "report", 0, 1),
        observed_at=now,
        publication_version=1,
    )
    return old, newer, now


def test_report_cleanup_preserves_publications_and_requirement_watermarks(
    session, sync_seed
):
    old, newer, now = pair(session, sync_seed)
    before = [
        (q.id, q.desired_revision, q.satisfied_revision)
        for q in session.exec(select(SyncRequirement)).all()
    ]
    assert compact(session, old.id, now)
    session.refresh(old)
    assert old.status == "CANCELLED" and old.error_code == "requirement_covered"
    assert [
        (q.id, q.desired_revision, q.satisfied_revision)
        for q in session.exec(select(SyncRequirement)).all()
    ] == before
    assert all(
        c.status == "SUPERSEDED"
        for c in session.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == old.id)
        ).all()
    )
    assert all(
        c.status == "PUBLISHED"
        for c in session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == newer.id
            )
        ).all()
    )
    assert not compact(session, old.id, now)


@pytest.mark.parametrize(
    "case",
    ["inflight", "running", "partial", "owner", "no_publication", "old_observation"],
)
def test_report_cleanup_rejects_missing_or_stale_proof(session, sync_seed, case):
    old, newer, now = pair(session, sync_seed)
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == newer.id)
    ).first()
    if case == "inflight":
        old.claimed_until = now + timedelta(minutes=1)
    elif case == "running":
        old.status = "RUNNING"
    elif case == "partial":
        session.get(SyncRequirement, child.requirement_id).satisfied_revision = 0
    elif case == "owner":
        session.get(SyncScope, child.scope_id).owner_epoch += 1
    elif case == "no_publication":
        child.status = "SUPERSEDED"
    elif case == "old_observation":
        child.observed_at = now - timedelta(days=1)
    session.flush()
    assert not compact(session, old.id, now)
    assert old.status in {"WAITING", "RUNNING"}


def test_cleanup_preserves_later_freshness_and_foreign_lease(session, sync_seed):
    from app.modules.sync_control.models import SyncSchedule

    old, newer, now = pair(session, sync_seed)
    third = ReportSyncJob(
        **dict(
            newer.model_dump(),
            id=uuid4(),
            job_key=str(uuid4()),
            created_at=now + timedelta(seconds=1),
        )
    )
    session.add(third)
    session.flush()
    adopt_report_job(session, third)
    head = session.exec(select(SyncSchedule)).first()
    head.lease_execution_id, head.lease_execution_generation = third.id, 0
    head.lease_token, head.leased_until = uuid4(), now + timedelta(minutes=1)
    token = head.lease_token
    assert compact(session, old.id, now)
    assert head.lease_token == token and head.lease_execution_id == third.id
    assert head.active
    assert all(
        q.desired_revision > q.satisfied_revision
        for q in session.exec(select(SyncRequirement)).all()
    )


def test_cleanup_skips_foreign_locked_parent_without_waiting(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as session:
        old, _newer, now = pair(session, seed)
        identity = old.id
        session.commit()
    with factory() as holder, factory() as cleanup:
        holder.get(ReportSyncJob, identity, with_for_update=True)
        assert not compact(cleanup, identity, now)
