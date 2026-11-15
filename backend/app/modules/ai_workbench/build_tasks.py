"""Celery/Beat adapters for scheduled AI build occurrences."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from typing import cast as typing_cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.tasks import register_dispatch_task
from app.modules.ai_workbench.build_handoff import (
    OCCURRENCE_NAMESPACE,
    TASK_NAME,
    BuildHandoff,
)
from app.modules.ai_workbench.models import AIOptimizer, BuildOccurrence
from app.modules.ai_workbench.plans import AIDailyPlanRow

register_dispatch_task(TASK_NAME, "builds")
PLAN_DISPATCH_TASK = "ai_workbench.dispatch_due_plans"
register_dispatch_task(PLAN_DISPATCH_TASK, "control")


def dispatch_occurrence_task(
    *, database_engine: Any, tenant_id: UUID, actor_id: UUID, payload: dict[str, Any]
) -> None:
    if set(payload) != {"occurrence_id"}:
        raise DomainError("dispatch_payload_invalid", "AI 搭建发生记录投递参数无效")
    try:
        occurrence_id = UUID(payload["occurrence_id"])
    except TypeError, ValueError, AttributeError:
        raise DomainError(
            "dispatch_payload_invalid", "AI 搭建发生记录标识无效"
        ) from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    with Session(database_engine) as session, session.begin():
        BuildHandoff(session, context=context).dispatch_occurrence(occurrence_id)


def dispatch_due_rows(session: Session, *, now: datetime, limit: int = 100) -> int:
    """只领取到期且未完成的行；历史终态不占用有界扫描的队头。"""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    same_row = (col(BuildOccurrence.tenant_id) == col(AIDailyPlanRow.tenant_id)) & (
        col(BuildOccurrence.plan_row_id) == col(AIDailyPlanRow.id)
    )
    statement = (
        select(AIDailyPlanRow)
        .join(
            AIOptimizer,
            (col(AIOptimizer.tenant_id) == col(AIDailyPlanRow.tenant_id))
            & (col(AIOptimizer.id) == col(AIDailyPlanRow.optimizer_id)),
        )
        .where(
            col(AIOptimizer.active).is_(True),
            col(AIDailyPlanRow.next_dispatch_at) <= now,
            func.timezone(
                col(AIDailyPlanRow.timezone),
                col(AIDailyPlanRow.business_date) + col(AIDailyPlanRow.scheduled_time),
            )
            <= now,
            or_(
                ~exists(select(col(BuildOccurrence.id)).where(same_row)),
                exists(
                    select(col(BuildOccurrence.id)).where(
                        same_row,
                        or_(
                            col(BuildOccurrence.status) == "WAITING_MATERIAL",
                            (col(BuildOccurrence.status) == "PENDING")
                            & col(BuildOccurrence.dispatch_id).is_(None),
                        ),
                    )
                ),
            ),
        )
        .order_by(
            col(AIDailyPlanRow.next_dispatch_at),
            col(AIDailyPlanRow.business_date),
            col(AIDailyPlanRow.scheduled_time),
            col(AIDailyPlanRow.id),
        )
        .limit(limit)
        .with_for_update(skip_locked=True, of=AIDailyPlanRow)
    )
    rows = session.exec(statement).all()
    count = 0
    for row in rows:
        blocked_code = None
        try:
            # 一行的撤权/失效不能回滚同批其他租户的排期。
            with session.begin_nested():
                optimizer = typing_cast(
                    AIOptimizer, session.get(AIOptimizer, row.optimizer_id)
                )

                context = TenantContext(
                    tenant_id=row.tenant_id,
                    actor_id=optimizer.created_by,
                    role="operator",
                )
                handoff = BuildHandoff(session, context=context)
                occurrence = session.exec(
                    select(BuildOccurrence)
                    .where(
                        col(BuildOccurrence.tenant_id) == row.tenant_id,
                        col(BuildOccurrence.plan_row_id) == row.id,
                    )
                    .with_for_update()
                ).first()
                if occurrence is None:
                    # 不跨当地日自动补跑；用户需要重新排期。
                    expired = (
                        now.astimezone(ZoneInfo(row.timezone)).date()
                        > row.business_date
                    )
                    occurrence = handoff.create_occurrence(
                        row.id,
                        uuid5(OCCURRENCE_NAMESPACE, f"scheduled:{row.id}"),
                        enqueue=not expired,
                        skip_reason="schedule_expired" if expired else None,
                    )
                elif now.astimezone(ZoneInfo(row.timezone)).date() > row.business_date:
                    occurrence.status = "FAILED"
                    occurrence.error_code = "schedule_expired"
                    session.add(occurrence)
                elif occurrence.status == "WAITING_MATERIAL":
                    handoff.schedule_retry(occurrence.id)
                elif occurrence.status == "PENDING" and occurrence.dispatch_id is None:
                    from app.jobs.outbox import enqueue_after_commit

                    occurrence.dispatch_id = enqueue_after_commit(
                        session,
                        context=context,
                        task_name=TASK_NAME,
                        task_key=f"ai-build-occurrence:{occurrence.id}",
                        payload={"occurrence_id": str(occurrence.id)},
                    )
                    session.add(occurrence)
                # 已投递的 PENDING 不再占据扫描队头；执行留在 builds 队列。
                count += 1
        except DomainError as error:
            blocked_code = error.code
            logging.getLogger(__name__).warning(
                "AI plan blocked: row=%s code=%s", row.id, error.code
            )
        # 领取结果无论成功、等待或撤权，都推进重试水位；未尝试的新行优先。
        row.next_dispatch_at = now + timedelta(seconds=60)
        row.last_dispatch_error = blocked_code
        session.add(row)
    return count


def dispatch_due_plans(*, database_engine: Any = engine, limit: int = 100) -> int:
    with Session(database_engine) as session, session.begin():
        return dispatch_due_rows(session, now=datetime.now(UTC), limit=limit)


@celery_app.task(
    bind=True,
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def dispatch_occurrence_celery(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    del self
    dispatch_occurrence_task(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
    )


@celery_app.task(name=PLAN_DISPATCH_TASK, time_limit=60, soft_time_limit=55)  # type: ignore[untyped-decorator]
def dispatch_due_plans_task() -> int:
    return dispatch_due_plans(database_engine=engine)


@celery_app.task(
    name="ai_workbench.schedule_reviews", time_limit=60, soft_time_limit=55
)
def schedule_reviews_task() -> int:
    from app.modules.ai_workbench.runtime import schedule_reviews

    with Session(engine) as session, session.begin():
        return schedule_reviews(session, now=datetime.now(UTC))


@celery_app.task(
    name="ai_workbench.scan_material_orders", time_limit=60, soft_time_limit=55
)
def scan_material_orders_task() -> int:
    from app.modules.ai_workbench.orders import scan_orders

    with Session(engine) as session, session.begin():
        return scan_orders(session, now=datetime.now(UTC))


@celery_app.task(
    name="ai_workbench.material_order",
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def material_order_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    from app.modules.ai_workbench.orders import process_order

    if set(payload) != {"order_id"}:
        raise DomainError("dispatch_payload_invalid", "素材订单任务参数无效")
    with Session(engine) as session, session.begin():
        process_order(
            session,
            context=TenantContext(
                tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role=""
            ),
            order_id=UUID(payload["order_id"]),
        )
