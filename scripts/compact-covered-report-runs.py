#!/usr/bin/env python3
"""清理被真实新发布完整覆盖的旧报表执行；默认预演回滚，不删除报表事实。"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.db import engine
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)
from app.modules.sync_control.schedule import record_report_outcome
from sqlalchemy import or_, text, update
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, col, select


def supersede_covered_report(session: Session, *, job_id: UUID, now: datetime) -> bool:
    job = session.exec(
        select(ReportSyncJob)
        .where(
            col(ReportSyncJob.id) == job_id,
            col(ReportSyncJob.contract_version) == 2,
            col(ReportSyncJob.status).in_(["QUEUED", "WAITING"]),
            or_(
                col(ReportSyncJob.claimed_until).is_(None),
                col(ReportSyncJob.claimed_until) <= now,
            ),
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if job is None:
        return False
    # 持父执行后只锁它的scope/引用；所有锁冲突都跳过，不能拖住正常发布。
    pending = session.exec(
        select(SyncExecutionScope).where(
            col(SyncExecutionScope.report_job_id) == job.id,
            col(SyncExecutionScope.status) == "PENDING",
        )
    ).all()
    if not pending:
        return False
    scope_ids = {child.scope_id for child in pending}
    scopes = session.exec(
        select(SyncScope)
        .where(col(SyncScope.id).in_(scope_ids))
        .order_by(
            col(SyncScope.tenant_id), col(SyncScope.advertiser_id), col(SyncScope.id)
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    if len(scopes) != len(scope_ids):
        return False
    by_scope = {scope.id: scope for scope in scopes}
    locked = session.exec(
        select(SyncExecutionScope)
        .where(col(SyncExecutionScope.id).in_([child.id for child in pending]))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    if len(locked) != len(pending):
        return False
    requirement_ids = {child.requirement_id for child in locked}
    requirements = session.exec(
        select(SyncRequirement)
        .where(col(SyncRequirement.id).in_(requirement_ids))
        .order_by(col(SyncRequirement.id))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    if len(requirements) != len(requirement_ids):
        return False
    by_requirement = {requirement.id: requirement for requirement in requirements}
    # satisfied计数只是候选条件；还须有同owner、同需求的真实发布引用证明。
    published = session.exec(
        select(SyncExecutionScope).where(
            col(SyncExecutionScope.scope_id).in_(scope_ids),
            col(SyncExecutionScope.requirement_id).in_(requirement_ids),
            col(SyncExecutionScope.status) == "PUBLISHED",
            col(SyncExecutionScope.tenant_id) == job.tenant_id,
        )
    ).all()
    evidence: dict[UUID, list[SyncExecutionScope]] = {}
    for proof in published:
        evidence.setdefault(proof.requirement_id, []).append(proof)
    for child in locked:
        scope = by_scope[child.scope_id]
        requirement = by_requirement[child.requirement_id]
        if (
            child.status != "PENDING"
            or child.execution_generation != job.claim_generation
            or scope.owner_epoch != job.owner_epoch
            or child.owner_epoch != job.owner_epoch
            or child.tenant_id != job.tenant_id
            or scope.tenant_id != job.tenant_id
            or scope.actor_id != job.actor_id
            or requirement.tenant_id != job.tenant_id
            or requirement.scope_id != child.scope_id
            or requirement.satisfied_revision < child.frozen_revision
        ):
            return False
        if not any(
            proof.owner_epoch == job.owner_epoch
            and proof.scope_id == child.scope_id
            and proof.frozen_revision >= child.frozen_revision
            and proof.observed_at is not None
            and proof.observed_at >= child.frozen_requested_after
            and proof.published_version is not None
            and proof.published_version > 0
            for proof in evidence.get(child.requirement_id, [])
        ):
            return False
    head_keys = session.exec(
        select(col(SyncSchedule.scope_id), col(SyncSchedule.work_class)).where(
            col(SyncSchedule.scope_id).in_(scope_ids)
        )
    ).all()
    heads = session.exec(
        select(SyncSchedule)
        .where(col(SyncSchedule.scope_id).in_(scope_ids))
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    if len(heads) != len(head_keys):
        return False
    session.execute(
        update(SyncExecutionScope)
        .where(col(SyncExecutionScope.id).in_([child.id for child in locked]))
        .values(status="SUPERSEDED")
    )
    job.status, job.error_code, job.finished_at = (
        "CANCELLED",
        "requirement_covered",
        now,
    )
    job.claim_token = job.claimed_until = None
    session.flush()
    # 沿用正式收尾：只释放本执行租约，保留后来新鲜度请求和其它执行的头。
    record_report_outcome(session, job)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--after", type=UUID)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--budget-seconds", type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000 or not 0 < args.budget_seconds <= 600:
        parser.error("limit must be 1..10000 and budget 0..600 seconds")
    stop = monotonic() + args.budget_seconds
    with Session(engine) as session:
        session.execute(text("SET LOCAL statement_timeout='5s'"))
        ids = (
            session.execute(
                text("""
          WITH covered AS MATERIALIZED (
            SELECT e.report_job_id id FROM sync_execution_scope e
            JOIN sync_requirement q ON q.id=e.requirement_id JOIN sync_scope s ON s.id=e.scope_id
            WHERE e.status='PENDING' AND e.report_job_id IS NOT NULL
            GROUP BY e.report_job_id
            HAVING bool_and(q.satisfied_revision>=e.frozen_revision AND e.owner_epoch=s.owner_epoch))
          SELECT j.id FROM covered c JOIN report_sync_job j ON j.id=c.id
          WHERE j.status IN ('QUEUED','WAITING') AND (j.claimed_until IS NULL OR j.claimed_until<=now())
          AND (CAST(:after AS uuid) IS NULL OR j.id>CAST(:after AS uuid))
          ORDER BY j.id LIMIT :limit
        """),
                {"limit": args.limit, "after": args.after},
            )
            .scalars()
            .all()
        )
    counts = {
        "examined": 0,
        "superseded" if args.apply else "would_supersede": 0,
        "deferred": 0,
    }
    last_id = args.after
    for identity in ids:
        if monotonic() >= stop:
            break
        last_id = identity
        try:
            with Session(engine) as session:
                session.execute(text("SET LOCAL statement_timeout='3s'"))
                session.execute(text("SET LOCAL lock_timeout='100ms'"))
                changed = supersede_covered_report(
                    session, job_id=identity, now=datetime.now(UTC)
                )
                session.commit() if args.apply else session.rollback()
                counts["examined"] += 1
                counts["superseded" if args.apply else "would_supersede"] += int(
                    changed
                )
        except DBAPIError:
            counts["deferred"] += 1
    print(
        json.dumps(
            {
                "apply": args.apply,
                "last_id": str(last_id) if last_id else None,
                **counts,
                "remaining_candidates": len(ids)
                - counts["examined"]
                - counts["deferred"],
            }
        )
    )


if __name__ == "__main__":
    main()
