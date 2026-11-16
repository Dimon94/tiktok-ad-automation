"""Small DSH task state machine with tenant/optimizer fences.

The DSH worker receives a short-lived authorization and an opaque fence.  It
never receives provider credentials or database access and can only return the
structured evidence/proposal payload persisted by the review service.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.ai_workbench.reviews import DSHReviewTask, authorize


def _task(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    task_id: UUID,
    lock: bool = False,
) -> DSHReviewTask:
    query = select(DSHReviewTask).where(
        DSHReviewTask.tenant_id == context.tenant_id,
        DSHReviewTask.optimizer_id == optimizer_id,
        DSHReviewTask.id == task_id,
    )
    if lock:
        query = query.with_for_update()
    row = session.exec(query).one_or_none()
    if row is None:
        raise DomainError("ai_review_not_found", "ai_review_not_found")
    return row


def _check_fence(
    row: DSHReviewTask, *, fence: str, allow_cancelled_terminal: bool = False
) -> None:
    if fence != row.fence:
        raise DomainError("ai_task_fence_conflict", "DSH task fence is stale")
    if row.cancellation_requested and not (
        allow_cancelled_terminal and row.status == "CANCELLED"
    ):
        raise DomainError("ai_task_cancelled", "DSH task was cancelled")


def start_task(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    task_id: UUID,
    fence: str,
) -> DSHReviewTask:
    authorize(session, context)
    row = _task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id, lock=True
    )
    _check_fence(row, fence=fence)
    if row.authorization_expires_at <= datetime.now(UTC):
        raise DomainError("ai_dsh_authorization_expired", "DSH authorization expired")
    if row.status in {"COMPLETE", "CANCELLED"}:
        return row
    if row.status not in {"PENDING", "RUNNING"}:
        raise DomainError("ai_review_not_ready", "复盘证据尚未准备完成")
    row.status = "RUNNING"
    row.started_at = row.started_at or datetime.now(UTC)
    session.flush()
    return row


def finish_task(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    task_id: UUID,
    fence: str,
    result: dict[str, Any],
) -> DSHReviewTask:
    authorize(session, context)
    row = _task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id, lock=True
    )
    _check_fence(row, fence=fence)
    if row.status == "COMPLETE":
        return row
    if row.authorization_expires_at <= datetime.now(UTC):
        raise DomainError("ai_dsh_authorization_expired", "DSH authorization expired")
    # Keep only machine-readable output; model traces and secrets are removed.
    row.result = sanitize_public(result)
    row.status = "COMPLETE"
    row.finished_at = datetime.now(UTC)
    session.flush()
    return row


def cancel_task(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    task_id: UUID,
    fence: str,
) -> DSHReviewTask:
    authorize(session, context, write=True)
    row = _task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id, lock=True
    )
    _check_fence(row, fence=fence, allow_cancelled_terminal=True)
    if row.status in {"COMPLETE", "CANCELLED"}:
        return row
    row.cancellation_requested = True
    row.status = "CANCELLED"
    row.finished_at = datetime.now(UTC)
    session.flush()
    return row


def restart_task(
    session: Session, *, context: TenantContext, optimizer_id: UUID, task_id: UUID
) -> DSHReviewTask:
    """Fence a restart so an old worker cannot write a result after recovery."""
    authorize(session, context, write=True)
    row = _task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id, lock=True
    )
    if row.status in {"COMPLETE", "CANCELLED"}:
        return row
    row.restart_generation += 1
    row.fence = uuid4().hex
    if row.evidence.get("reading_contract") != "on-demand-v2":
        raise DomainError(
            "ai_review_contract_retired", "旧复盘合同已退役，请创建新复盘"
        )
    preparation = row.evidence.get("preparation", {})
    preparing = (
        row.status == "PREPARING"
        or not preparation.get("base_complete", False)
        or preparation.get("cursor", 0) < preparation.get("total_accounts", 0)
    )
    if preparing:
        row.evidence = {**row.evidence, "preparation": {**preparation, "attempts": 0}}
    row.status = "PREPARING" if preparing else "PENDING"
    row.cancellation_requested = False
    row.started_at = None
    row.finished_at = None
    row.result = {}
    # A restart gets a fresh short-lived worker grant.  Setting the expiry to
    # ``now`` would make every recovered task fail its own start fence.
    row.authorization_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    if preparing:
        from app.modules.ai_workbench.review_preparation import enqueue_preparation

        enqueue_preparation(session, task=row, repair=True)
    session.flush()
    return row
