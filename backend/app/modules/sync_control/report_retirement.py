"""有界巡检规范待办：被真实需求水位覆盖的旧执行退出，不改写事实或水位。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.schedule import record_report_outcome


def retire_covered_reports(
    session: Session,
    *,
    now: datetime,
    cursor: list[str] | None,
    scan_limit: int = 1000,
    batch_limit: int = 100,
) -> tuple[int, list[str] | None]:
    # 以PENDING部分索引的(scope,requirement)前缀推进；不遍历数百万终态任务。
    zero = UUID(int=0)
    position = [UUID(v) for v in cursor] if cursor else [zero, zero, zero]
    rows = (
        session.connection()
        .execute(
            text("""
      SELECT scope_id,requirement_id,id,report_job_id
      FROM sync_execution_scope
      WHERE status='PENDING' AND scope_id>=:scope
        AND (scope_id,requirement_id,id)>(:scope,:requirement,:reference)
      ORDER BY scope_id,requirement_id,id LIMIT :limit
    """),
            {
                "scope": position[0],
                "requirement": position[1],
                "reference": position[2],
                "limit": scan_limit,
            },
        )
        .all()
    )
    if not rows:
        return 0, None
    ids = list({r.report_job_id for r in rows if r.report_job_id is not None})
    jobs = (
        session.connection()
        .execute(
            text("""
      SELECT j.id FROM report_sync_job j
      WHERE j.id=ANY(CAST(:ids AS uuid[])) AND j.status IN ('QUEUED','WAITING','RUNNING')
        AND (j.claimed_until IS NULL OR j.claimed_until<=:now)
        AND EXISTS(SELECT 1 FROM sync_execution_scope e WHERE e.report_job_id=j.id AND e.status='PENDING')
        AND NOT EXISTS(
          SELECT 1 FROM sync_execution_scope e
          JOIN sync_requirement q ON q.id=e.requirement_id
          JOIN sync_scope s ON s.id=e.scope_id
          WHERE e.report_job_id=j.id AND (
            s.owner_epoch<>e.owner_epoch OR s.owner_epoch<>j.owner_epoch
            OR (e.status<>'PUBLISHED' AND q.satisfied_revision<e.frozen_revision)))
      ORDER BY j.id LIMIT :limit FOR UPDATE OF j SKIP LOCKED
    """),
            {"ids": ids, "now": now, "limit": batch_limit},
        )
        .scalars()
        .all()
        if ids
        else []
    )
    for identity in jobs:
        job = session.get(ReportSyncJob, identity, populate_existing=True)
        assert job is not None
        job.status, job.error_code, job.finished_at = (
            "CANCELLED",
            "coverage_superseded",
            now,
        )
        job.claim_token = job.claimed_until = None
        session.add(job)
        # 沿用围栏及head释放；任何另一任务持有的租约均不被覆盖。
        record_report_outcome(session, job)
    # 批达到上限时重读此页，避免跳过同页尚未领取的可收尾执行。
    next_cursor = cursor if len(jobs) == batch_limit else [str(v) for v in rows[-1][:3]]
    return len(jobs), next_cursor
