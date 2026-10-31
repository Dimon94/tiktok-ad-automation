"""旧清理接入新子桶引用：先保留缺口，再有界移除已结束执行。"""

from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.modules.reporting import job_tasks
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncPage
from app.modules.sync_control.models import SyncExecutionScope
from app.modules.sync_control.report_demand import adopt_report_job
from tests.modules.sync_control.test_cutover import legacy_job
from tests.modules.sync_control.test_retention import seed_dispatch


def terminal_job(session, seed):
    job, page = legacy_job(session, seed)
    adopt_report_job(session, job)
    job.status = "COMPLETE"
    job.finished_at = datetime.now(UTC) - timedelta(days=8)
    session.flush()
    return job, page


def test_pending_child_preserves_expired_terminal_job_and_private_page(
    session, sync_seed
):
    job, page = terminal_job(session, sync_seed)
    result = job_tasks.cleanup_store(
        session,
        now=datetime.now(UTC),
        categories=("material_pages", "jobs"),
        cursors={},
    )
    assert result == {"material_pages": 0, "jobs": 0}
    assert session.get(ReportSyncJob, job.id) and session.get(
        ReportSyncPage, (job.id, 1)
    )


def test_short_page_cleanup_preserves_online_summary_and_children(
    session, sync_seed, monkeypatch
):
    job, page = terminal_job(session, sync_seed)
    for child in session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job.id)
    ).all():
        child.status = "PUBLISHED"
    session.flush()
    monkeypatch.setattr(job_tasks, "CLEANUP_JOB_BATCH_ROWS", 1)
    now = datetime.now(UTC)
    for _ in range(4):
        job_tasks.cleanup_store(
            session, now=now, categories=("material_pages", "jobs"), cursors={}
        )
    identifier = page.job_id
    session.expire_all()
    assert session.get(ReportSyncJob, identifier) is not None
    assert session.get(ReportSyncPage, (identifier, 1)) is None
    assert (
        session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == identifier
            )
        ).first()
        is not None
    )


def test_uncompacted_dispatch_retains_owner_until_identity_is_preserved(
    session, sync_seed
):
    job, row, *_ = seed_dispatch(session, sync_seed, published=False)
    job_tasks.cleanup_store(
        session, now=datetime.now(UTC), categories=("jobs",), cursors={}
    )
    session.expire_all()
    assert session.get(ReportSyncJob, job.id) is not None and row.payload


def test_successful_pages_expire_after_one_hour_without_removing_replay_identity(
    session, sync_seed
):
    job, dispatch, *_ = seed_dispatch(session, sync_seed)
    job.finished_at = datetime.now(UTC) - timedelta(hours=2)
    page = ReportSyncPage(
        job_id=job.id,
        page=1,
        tenant_id=job.tenant_id,
        scope_key="finished",
        rows=[],
        row_keys=[],
        total_page=1,
        total_number=0,
    )
    session.add(page)
    session.flush()
    result = job_tasks.cleanup_store(
        session, now=datetime.now(UTC), categories=("material_pages",), cursors={}
    )
    session.expire_all()
    assert result["material_pages"] == 1
    assert session.get(ReportSyncPage, (job.id, 1)) is None
    assert session.get(ReportSyncJob, job.id) is not None
    assert dispatch.payload and dispatch.compacted_at is None


def test_recent_success_and_recent_failure_keep_private_pages(session, sync_seed):
    for status, age in [
        ("COMPLETE", timedelta(minutes=30)),
        ("FAILED", timedelta(hours=2)),
    ]:
        job, _, *_ = seed_dispatch(session, sync_seed, status=status)
        job.finished_at = datetime.now(UTC) - age
        session.add(
            ReportSyncPage(
                job_id=job.id,
                page=1,
                tenant_id=job.tenant_id,
                scope_key="retained",
                rows=[],
                row_keys=[],
                total_page=1,
                total_number=0,
            )
        )
    session.flush()
    result = job_tasks.cleanup_store(
        session, now=datetime.now(UTC), categories=("material_pages",), cursors={}
    )
    assert result["material_pages"] == 0


def test_successful_page_with_unexpired_lease_is_preserved(session, sync_seed):
    job, _, *_ = seed_dispatch(session, sync_seed)
    job.finished_at = datetime.now(UTC) - timedelta(hours=2)
    job.claimed_until = datetime.now(UTC) + timedelta(minutes=5)
    session.add(
        ReportSyncPage(
            job_id=job.id,
            page=1,
            tenant_id=job.tenant_id,
            scope_key="lease",
            rows=[],
            row_keys=[],
            total_page=1,
            total_number=0,
        )
    )
    session.flush()
    result = job_tasks.cleanup_store(
        session, now=datetime.now(UTC), categories=("material_pages",), cursors={}
    )
    assert result["material_pages"] == 0
    assert session.get(ReportSyncPage, (job.id, 1)) is not None
