from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement
from tests.modules.reporting.test_jobs import _route


def test_retire_only_fully_covered_unclaimed_reports(session, sync_seed):
    from app.modules.sync_control.report_retirement import retire_covered_reports

    now = datetime.now(UTC)
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        actor_id=sync_seed.actor_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="history",
        advertiser_ids=["acc-1"],
        start=now.date() - timedelta(days=2),
        end=now.date() - timedelta(days=1),
        frozen_route=_route(sync_seed),
    )
    job = session.get(ReportSyncJob, job_id)
    refs = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job.id)
    ).all()
    assert len(refs) == 2
    first = session.get(SyncRequirement, refs[0].requirement_id)
    first.satisfied_revision = first.desired_revision
    session.flush()
    assert retire_covered_reports(session, now=now, cursor=None)[0] == 0
    second = session.get(SyncRequirement, refs[1].requirement_id)
    second.satisfied_revision = second.desired_revision
    job.claimed_until = now + timedelta(minutes=1)
    session.flush()
    assert retire_covered_reports(session, now=now, cursor=None)[0] == 0
    job.claimed_until = None
    session.flush()
    assert retire_covered_reports(session, now=now, cursor=None)[0] == 1
    session.refresh(job)
    assert job.status == "CANCELLED" and job.error_code == "coverage_superseded"
    for ref in refs:
        session.refresh(ref)
        assert ref.status == "SUPERSEDED"
    assert second.satisfied_revision == second.desired_revision
