"""Celery管理编排；物理请求由进程共享窗口及平台额度调度。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as DatabasePoolTimeout
from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.ad_management.execution import _recover_cancelled_claims, _update_task
from app.modules.ad_management.models import ManagementTask, ManagementTaskItem

MANAGEMENT_DISPATCH_TASK = "ad_management.execute"
register_dispatch_task(MANAGEMENT_DISPATCH_TASK, "ad-management")


def _payload(payload: dict[str, Any]) -> tuple[UUID, int]:
    validate_dispatch_payload(payload)
    if set(payload) != {"task_id", "generation"}:
        raise DomainError("dispatch_payload_invalid", "管理任务参数无效")
    try:
        task_id = UUID(str(payload["task_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "管理任务标识无效") from exc
    generation = payload["generation"]
    if type(generation) is not int or generation < 1:
        raise DomainError("dispatch_payload_invalid", "管理任务领取代数无效")
    return task_id, generation


def _locked_task(session: Session, task_id: UUID, tenant: UUID, actor: UUID):
    # 入口、取消与回执统一先锁父任务，禁止旧状态在并发取消后写回RUNNING。
    return session.exec(
        select(ManagementTask)
        .where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == tenant,
            ManagementTask.actor_id == actor,
        )
        .with_for_update()
    ).one_or_none()


@celery_app.task(
    name=MANAGEMENT_DISPATCH_TASK,
    autoretry_for=(OperationalError, InterfaceError, DatabasePoolTimeout, RedisError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": None},
)
def execute_management_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> str:
    try:
        tenant = UUID(tenant_id)
        actor = UUID(actor_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "管理任务操作者无效") from exc
    from app.modules.automation_rules.results import refresh_execution_results

    task_id, _dispatch_generation = _payload(payload)
    with Session(engine) as session:
        task = _locked_task(session, task_id, tenant, actor)
        if task is None:
            raise DomainError("resource_not_found", "管理任务不存在")
        if task.source_kind == "DIRECT" and task.status == "PREPARING":
            from app.modules.ad_management.preparation import prepare_direct_task

            session.close()
            prepare_direct_task(engine, task_id)
            task = _locked_task(session, task_id, tenant, actor)
            if task is None:
                return "FAILED"
        if task.status == "PREPARING":
            return task.status
        if task.status == "CANCELLED":
            _recover_cancelled_claims(session, task)
            _update_task(session, task.id, task.tenant_id)
        _queue_unknown_observations(session, task)
        refresh_pending = session.exec(
            select(ManagementTaskItem.id).where(
                ManagementTaskItem.task_id == task_id,
                ManagementTaskItem.tenant_id == tenant,
                ManagementTaskItem.execution_result == "ACCEPTED",
                ManagementTaskItem.observation_state == "REFRESH_PENDING",
            )
        ).first()
        if (
            task.status
            in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "NEEDS_REVIEW"}
            and refresh_pending is None
        ):
            refresh_execution_results(session, task_id, tenant)
            session.commit()
            return task.status
        cancelled = task.status == "CANCELLED"
        if not cancelled:
            task.status = "RUNNING"
            from app.modules.ad_management.recovery_dispatch import ensure_future_wakeup

            ensure_future_wakeup(session, task)
        session.add(task)
        session.commit()
        item_ids = [
            row.id
            for row in session.exec(
                select(ManagementTaskItem)
                .where(
                    ManagementTaskItem.task_id == task_id,
                    ManagementTaskItem.tenant_id == tenant,
                )
                .order_by(col(ManagementTaskItem.position))
            ).all()
            if not cancelled
            or (
                row.execution_result == "ACCEPTED"
                and row.observation_state == "REFRESH_PENDING"
            )
        ]
    from app.modules.ad_management.dispatch_work import execute_task_items

    completed = execute_task_items(engine, tuple(item_ids))
    if not completed:
        # 祖先锁竞争没有真实发送，延时重排任务，不消耗传输重试预算。
        from datetime import UTC, datetime, timedelta
        from uuid import uuid4

        from app.core.context import TenantContext
        from app.jobs.models import PendingDispatch
        from app.jobs.outbox import enqueue_after_commit

        with Session(engine) as deferred, deferred.begin():
            current = _locked_task(deferred, task_id, tenant, actor)
            if current is not None and current.status != "CANCELLED":
                dispatch_id = enqueue_after_commit(
                    deferred,
                    context=TenantContext(tenant, actor, "operator"),
                    task_name=MANAGEMENT_DISPATCH_TASK,
                    task_key=f"management-lock-wait:{task_id}:{uuid4()}",
                    payload={
                        "task_id": str(task_id),
                        "generation": _dispatch_generation,
                    },
                )
                dispatch = deferred.get(PendingDispatch, dispatch_id)
                dispatch.available_at = datetime.now(UTC) + timedelta(seconds=1)
                deferred.add(dispatch)
    with Session(engine) as session:
        task = _locked_task(session, task_id, tenant, actor)
        if task is None:
            return "FAILED"
        # 与单项回写共用汇总规则：不可重试的未发送项必须结束任务。
        _update_task(session, task_id, tenant)
        _queue_unknown_observations(session, task)
        session.flush()
        refresh_execution_results(session, task_id, tenant)
        session.commit()
        return task.status


__all__ = ["MANAGEMENT_DISPATCH_TASK", "execute_management_task"]


def _queue_unknown_observations(session: Session, task: ManagementTask):
    from sqlalchemy import or_

    from app.modules.ad_management.reconciliation import queue_reconciliation

    ids = tuple(
        session.exec(
            select(ManagementTaskItem.id)
            .where(
                ManagementTaskItem.task_id == task.id,
                ManagementTaskItem.tenant_id == task.tenant_id,
                ManagementTaskItem.execution_result == "UNKNOWN",
                or_(
                    col(ManagementTaskItem.observation_state).is_(None),
                    ManagementTaskItem.observation_state == "NEEDS_REVIEW",
                ),
            )
            .order_by(ManagementTaskItem.id)
        ).all()
    )
    if ids:
        queue_reconciliation(session, task, ids)


# worker重启即注册只读核查任务，不依赖某次业务请求先触发导入。
from app.modules.ad_management import (  # noqa: E402
    reconciliation_tasks as reconciliation_tasks,  # noqa: F401
)
