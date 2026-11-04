"""到期头只包含仍有未满足需求的账户scope，不扫描已终结执行历史。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.integrations.tiktok.platform_quotas import sdk_endpoint
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import (
    ReportBalanceRun,
    ReportSyncJob,
)
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)


def report_endpoint(kind: str) -> str:
    return sdk_endpoint(
        {
            "material_day": "reports.material_breakdown",
            "material_links": "reports.material_overview",
        }.get(kind, "reports.integrated")
    )


def report_class(kind: str, priority: int) -> str:
    if kind == "material_links":
        return "directory"
    return "realtime" if priority <= 2 else "history"


def wake_scope(
    session: Session,
    *,
    scope: SyncScope,
    work_class: str,
    endpoint: str,
    due_at: datetime,
    oldest_pending_at: datetime | None,
) -> None:
    row = SyncSchedule(
        scope_id=scope.id,
        tenant_id=scope.tenant_id,
        work_class=work_class,
        physical_endpoint=endpoint,
        next_eligible_at=due_at,
        oldest_pending_at=oldest_pending_at,
    )
    statement = insert(SyncSchedule).values(**row.model_dump())
    session.connection().execute(
        statement.on_conflict_do_update(
            index_elements=["scope_id", "work_class"],
            set_={
                "active": True,
                "next_eligible_at": func.least(
                    col(SyncSchedule.next_eligible_at), due_at
                ),
                "oldest_pending_at": func.least(
                    col(SyncSchedule.oldest_pending_at), oldest_pending_at
                ),
            },
        )
    )


def record_report_outcome(session: Session, job: ReportSyncJob) -> None:
    terminal = retire_execution_children(session, job, "report")

    heads = session.exec(
        select(SyncSchedule)
        .join(
            SyncExecutionScope,
            col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
        )
        .where(col(SyncExecutionScope.report_job_id) == job.id)
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .with_for_update(of=SyncSchedule)
        .execution_options(populate_existing=True)
    ).all()
    heads = list({(row.scope_id, row.work_class): row for row in heads}.values())
    due_by_head = pending_execution_dues(
        session, heads, "report", now=datetime.now(UTC)
    )
    for head in heads:
        if head.lease_execution_id is not None and (
            head.lease_execution_id != job.id
            or head.lease_execution_generation != job.claim_generation
        ):
            continue
        head.leased_until = head.lease_token = head.lease_execution_id = (
            head.lease_execution_generation
        ) = None
        head.wait_reason = (
            "QUOTA"
            if job.error_code == "admission_deferred"
            else "RETRY"
            if job.error_code
            else None
        )
        due = due_by_head.get((head.scope_id, head.work_class))
        head.active = due is not None
        if due is not None:
            head.next_eligible_at = due
        if terminal and not head.active:
            head.wait_reason = terminal


def retire_execution_children(
    session: Session,
    execution: ReportSyncJob | ReportBalanceRun | AdDirectoryRun,
    kind: str,
) -> str | None:
    """结束执行引用而不伪造需求覆盖；缺口留存，后续显式/周期刷新仍可重建。"""
    if execution.status not in {"COMPLETE", "FAILED", "CANCELLED", "STALE"}:
        return None
    column = {
        "report": col(SyncExecutionScope.report_job_id),
        "directory": col(SyncExecutionScope.directory_run_id),
        "balance": col(SyncExecutionScope.balance_run_id),
    }[kind]
    rows = session.exec(
        select(SyncExecutionScope)
        .where(
            column == execution.id,
            col(SyncExecutionScope.status) == "PENDING",
            col(SyncExecutionScope.execution_generation) == execution.claim_generation,
            col(SyncExecutionScope.owner_epoch) == execution.owner_epoch,
        )
        .with_for_update()
    ).all()
    for child in rows:
        child.status = "SUPERSEDED"
    session.flush()
    if rows:
        reason = execution.error_code or (
            "coverage_unproven"
            if execution.status == "COMPLETE"
            else execution.status.lower()
        )
        return f"TERMINAL:{reason}"[:32]
    return None


def has_pending_execution(session: Session, head: SyncSchedule) -> bool:
    return (
        session.exec(
            select(col(SyncExecutionScope.id))
            .join(
                SyncRequirement,
                col(SyncRequirement.id) == col(SyncExecutionScope.requirement_id),
            )
            .join(SyncScope, col(SyncScope.id) == col(SyncExecutionScope.scope_id))
            .where(
                col(SyncExecutionScope.scope_id) == head.scope_id,
                col(SyncExecutionScope.status) == "PENDING",
                col(SyncExecutionScope.owner_epoch) == col(SyncScope.owner_epoch),
                col(SyncRequirement.source) == head.work_class,
                col(SyncRequirement.satisfied_revision)
                < col(SyncRequirement.desired_revision),
            )
            .limit(1)
        ).first()
        is not None
    )


def wake_report_job(session: Session, job: ReportSyncJob) -> None:
    """依赖变更只提前到期头，不释放另一执行已占用的租约。"""
    for head in session.exec(
        select(SyncSchedule)
        .join(
            SyncExecutionScope,
            col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
        )
        .where(
            col(SyncExecutionScope.report_job_id) == job.id,
        )
    ).all():
        head.active = has_pending_execution(session, head)
        if head.active:
            head.next_eligible_at = min(head.next_eligible_at, job.due_at)
            head.wait_reason = None


def pending_execution_dues(
    session: Session,
    heads: Sequence[SyncSchedule],
    kind: str,
    *,
    now: datetime,
    respect_claims: bool = False,
) -> dict[tuple[UUID, str], datetime]:
    """一页退避不能推迟同scope另一到期桶；同时排除失去任何子scope的旧批次。"""
    from app.modules.sync_control.requirements import execution_owner_current

    models: dict[
        str,
        tuple[
            type[ReportSyncJob] | type[AdDirectoryRun] | type[ReportBalanceRun],
            str,
            str,
            tuple[str, ...],
        ],
    ] = {
        "report": (
            ReportSyncJob,
            "report_job_id",
            "due_at",
            ("QUEUED", "WAITING", "RUNNING"),
        ),
        "directory": (
            AdDirectoryRun,
            "directory_run_id",
            "next_attempt_at",
            ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE"),
        ),
        "balance": (ReportBalanceRun, "balance_run_id", "next_attempt_at", ("ACTIVE",)),
    }
    if not heads:
        return {}
    model, reference, due_field, statuses = models[kind]
    # 同一BASIC/余额批次可能含数千账户；先按执行ID去重，整批owner只核验一次。
    candidates = (
        select(getattr(SyncExecutionScope, reference).label("id"))
        .where(
            col(SyncExecutionScope.scope_id).in_({head.scope_id for head in heads}),
            col(SyncExecutionScope.status) == "PENDING",
            getattr(SyncExecutionScope, reference).is_not(None),
        )
        .distinct()
        .cte("pending_executions")
        .prefix_with("MATERIALIZED")
    )
    valid = (
        select(col(model.id).label("id"))
        .join(candidates, candidates.c.id == col(model.id))
        .where(
            col(model.status).in_(statuses), execution_owner_current(model, reference)
        )
        .cte("valid_executions")
        .prefix_with("MATERIALIZED")
    )
    # 只有领取失败的资格纠正才尊重有效worker租约。正常outcome在调用后才清
    # 当前租约，不能把即将释放的租约写成下一次due、延迟正常分页和短退避。
    due = (
        func.greatest(
            func.coalesce(getattr(model, due_field), now),
            func.coalesce(col(ReportSyncJob.claimed_until), now),
        )
        if kind == "report" and respect_claims
        else func.coalesce(getattr(model, due_field), now)
    )
    rows = session.exec(
        select(
            col(SyncExecutionScope.scope_id),
            col(SyncRequirement.source),
            func.min(due),
        )
        .select_from(SyncExecutionScope)
        .join(model, getattr(SyncExecutionScope, reference) == col(model.id))
        .join(valid, valid.c.id == col(model.id))
        .join(
            SyncRequirement,
            col(SyncRequirement.id) == col(SyncExecutionScope.requirement_id),
        )
        .where(
            col(SyncExecutionScope.scope_id).in_({head.scope_id for head in heads}),
            col(SyncExecutionScope.status) == "PENDING",
            col(SyncRequirement.satisfied_revision)
            < col(SyncRequirement.desired_revision),
        )
        .group_by(col(SyncExecutionScope.scope_id), col(SyncRequirement.source))
    ).all()
    return {(scope_id, work_class): due for scope_id, work_class, due in rows}
