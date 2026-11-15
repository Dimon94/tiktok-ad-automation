"""与检查点同事务保留恢复消息；提前重投也不能吞掉未来的唤醒。"""

from datetime import UTC, datetime, timedelta
from math import ceil
from typing import cast as typing_cast

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)


def schedule_wakeup(
    session: Session,
    task: ManagementTask,
    due: datetime,
    *,
    kind: str,
    quantum: int = 1,
):
    bucket = ceil(due.timestamp() / quantum)
    # 同一秒/租约时间桶仅一个任务级消息；payload固定，不按成员代数拆分派发。
    identity = enqueue_after_commit(
        session,
        context=TenantContext(task.tenant_id, task.actor_id, "operator"),
        task_name="ad_management.execute",
        task_key=f"ad-management-wake:{task.id}:{kind}:{bucket}",
        payload={"task_id": str(task.id), "generation": 1},
    )
    row = typing_cast(PendingDispatch, session.get(PendingDispatch, identity))

    row.available_at = datetime.fromtimestamp(bucket * quantum, UTC)
    session.add(row)
    return identity


def ensure_future_wakeup(session: Session, task: ManagementTask):
    if task.status in {"CANCELLED", "PREPARING"}:
        return
    now = datetime.now(UTC)
    claimed_until = session.exec(
        select(func.min(col(ManagementTaskItem.claimed_until))).where(
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.tenant_id) == task.tenant_id,
            col(ManagementTaskItem.claim_token).is_not(None),
            col(ManagementTaskItem.claimed_until) > now,
            col(ManagementTaskItem.execution_result) == "PENDING",
        )
    ).one()
    if claimed_until is not None:
        schedule_wakeup(
            session,
            task,
            claimed_until + timedelta(seconds=1),
            kind="claim",
            quantum=30,
        )
    latest = (
        select(func.max(col(ManagementRequestAttempt.attempt)))
        .where(
            col(ManagementRequestAttempt.task_item_id) == col(ManagementTaskItem.id),
            col(ManagementRequestAttempt.tenant_id) == task.tenant_id,
        )
        .correlate(ManagementTaskItem)
        .scalar_subquery()
    )
    responses = session.exec(
        select(col(ManagementRequestAttempt.response))
        .join(
            ManagementTaskItem,
            col(ManagementTaskItem.id) == col(ManagementRequestAttempt.task_item_id),
        )
        .where(
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.tenant_id) == task.tenant_id,
            col(ManagementRequestAttempt.attempt) == latest,
            col(ManagementRequestAttempt.retryable).is_(True),
            col(ManagementTaskItem.execution_result) == "NOT_SENT",
        )
    ).all()
    due_dates = []
    for response in responses:
        try:
            due = datetime.fromisoformat(response["retry_at"])
            if due > now:
                due_dates.append(due)
        except KeyError, ValueError, TypeError:
            continue
    if due_dates:
        # 最早到期项先恢复；每次提前投递仍会再次登记剩余未来项。
        schedule_wakeup(session, task, min(due_dates), kind="retry")
