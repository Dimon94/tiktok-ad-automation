"""到期头只包含仍有未满足需求的账户scope，不扫描已终结执行历史。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, func
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


def report_endpoint(kind: str, progress: Mapping | None = None) -> str:
    if kind != "operation_log":
        return sdk_endpoint("reports.integrated")
    # 阶段属于持久执行，不能用报表kind把轮询/下载继续记到创建接口。
    # 无效阶段仍交给worker的原错误处理，不让发现头因解析异常丢失需求。
    state = (progress or {}).get("operation_log", {})
    phase = state.get("phase", "CREATE") if isinstance(state, Mapping) else "CREATE"
    phase = phase if isinstance(phase, str) else "CREATE"
    operation = {"CHECK": "changelog.check", "DOWNLOAD": "changelog.download"}.get(
        phase, "changelog.create"
    )
    return sdk_endpoint(operation)


def operation_log_order():
    """同账户日志先昨日增量、再较近历史；其他报表保持原有顺序。"""
    return (
        case(
            (col(ReportSyncJob.kind) == "operation_log", col(ReportSyncJob.priority)),
            else_=0,
        ),
        case(
            (col(ReportSyncJob.kind) == "operation_log", col(ReportSyncJob.end_date)),
            else_=None,
        ).desc(),
    )


def report_class(_kind: str, priority: int) -> str:
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
                "last_dispatched_at": case(
                    (col(SyncSchedule.work_class) == "realtime", None),
                    else_=col(SyncSchedule.last_dispatched_at),
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
            if job.kind == "operation_log":
                endpoint = _pending_operation_endpoint(
                    session, head, now=datetime.now(UTC)
                )
                if endpoint is not None:
                    head.physical_endpoint = endpoint
        if terminal and not head.active:
            head.wait_reason = terminal


def _pending_operation_endpoint(
    session: Session, head: SyncSchedule, *, now: datetime
) -> str | None:
    """单scope可有多个阶段；路由属于下一有效候选，而非刚退出的那条执行。"""
    from app.modules.sync_control.requirements import execution_owner_current

    job = session.exec(
        select(ReportSyncJob)
        .join(
            SyncExecutionScope,
            col(SyncExecutionScope.report_job_id) == col(ReportSyncJob.id),
        )
        .join(
            SyncRequirement,
            col(SyncRequirement.id) == col(SyncExecutionScope.requirement_id),
        )
        .where(
            col(SyncExecutionScope.scope_id) == head.scope_id,
            col(SyncExecutionScope.status) == "PENDING",
            col(SyncRequirement.source) == head.work_class,
            col(SyncRequirement.satisfied_revision)
            < col(SyncRequirement.desired_revision),
            col(ReportSyncJob.status).in_(("QUEUED", "WAITING", "RUNNING")),
            execution_owner_current(ReportSyncJob, "report_job_id"),
        )
        .order_by(
            # 已到期候选保持dispatcher的revision/日志优先级次序；没有到期任务
            # 时才按最早due选下一阶段，不能让刚轮询的30秒等待挡住另一日桶。
            case((col(ReportSyncJob.due_at) <= now, 0), else_=1),
            case(
                (col(ReportSyncJob.due_at) > now, col(ReportSyncJob.due_at)), else_=None
            ),
            case(
                (
                    col(SyncExecutionScope.frozen_revision)
                    == col(SyncRequirement.desired_revision),
                    0,
                ),
                else_=1,
            ),
            *operation_log_order(),
            col(ReportSyncJob.created_at),
            col(ReportSyncJob.id),
        )
        .limit(1)
    ).first()
    return report_endpoint(job.kind, job.progress) if job is not None else None


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
        # 目录未设退避即从创建时到期，不能每次outcome向当前时刻推移；
        # 否则并发完成会把另一个已开始的补位轮次挡在未来due之外。
        else func.coalesce(getattr(model, due_field),
                           col(AdDirectoryRun.created_at) if kind == "directory" else now)
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


def check_report_grant(session: Session, job: ReportSyncJob, token: UUID) -> None:
    """网关打开失败亦不可清除已被后继执行替换的调度租约。"""
    from app.modules.sync_control.executions import ExecutionLost

    heads = session.exec(
        select(SyncSchedule)
        .join(
            SyncExecutionScope,
            col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
        )
        .join(
            SyncRequirement,
            col(SyncRequirement.id) == col(SyncExecutionScope.requirement_id),
        )
        .where(
            col(SyncExecutionScope.report_job_id) == job.id,
            col(SyncSchedule.tenant_id) == job.tenant_id,
            col(SyncSchedule.work_class) == col(SyncRequirement.source),
        )
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .with_for_update(of=SyncSchedule)
        .execution_options(populate_existing=True)
    ).all()
    now = datetime.now(UTC)
    if not heads or any(
        head.lease_token != token
        or head.lease_execution_id != job.id
        or head.lease_execution_generation != job.claim_generation
        or head.leased_until is None
        or head.leased_until <= now
        for head in heads
    ):
        raise ExecutionLost()
