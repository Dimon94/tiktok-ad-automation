"""Bounded local submission expansion; existing current delivery is repaired."""

from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import literal
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.builds.execution_models import Submission, SubmissionUnit

TASK_NAME = "builds.expand_submission"
register_dispatch_task(TASK_NAME, "builds")
UNIT_TASK_NAME = "builds.execute_unit"
register_dispatch_task(UNIT_TASK_NAME, "builds")
REPAIR_SECONDS = 120
MAX_DELIVERY_PAGES = 100
MAX_DELIVERY_SECONDS = 10.0
PAGE_SIZE = 100


def queue_expansion(session: Session, row: Submission) -> None:
    row.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"build-submission:{row.id}:{row.dispatch_revision}",
        payload={"submission_id": str(row.id), "revision": row.dispatch_revision},
    )
    row.repair_after = datetime.now(UTC) + timedelta(seconds=REPAIR_SECONDS)
    session.add(row)


def process_submission(
    *,
    database_engine: Any,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
    dispatch_id: UUID | None = None,
) -> None:
    """Commit small pages under the original delivery fence, then yield fairly.

    Internal callers may omit dispatch_id; the Celery boundary always supplies
    its real message ID. A crash between commits retains the current generation
    for at-least-once replay and the existing publication watchdog.
    """
    from app.modules.builds.submissions import expand_submission

    if (
        set(payload) != {"submission_id", "revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "提交展开参数无效")
    try:
        identity = UUID(payload["submission_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "提交展开参数无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    deadline = monotonic() + MAX_DELIVERY_SECONDS
    for page in range(MAX_DELIVERY_PAGES):
        with Session(database_engine) as session, session.begin():
            row = session.exec(
                select(Submission)
                .where(Submission.tenant_id == tenant_id, Submission.id == identity)
                .with_for_update(key_share=True)
            ).one_or_none()
            if row is None or row.actor_id != actor_id:
                raise DomainError("resource_not_found", "提交任务不存在")
            if row.expanded or row.dispatch_revision != payload["revision"]:
                return
            if dispatch_id is None:
                dispatch_id = row.dispatch_id
            if dispatch_id is None or dispatch_id != row.dispatch_id:
                return
            delivery = session.get(PendingDispatch, dispatch_id)
            if delivery is None or (
                delivery.tenant_id,
                delivery.actor_id,
                delivery.task_name,
                delivery.payload,
            ) != (tenant_id, actor_id, TASK_NAME, payload):
                raise DomainError("dispatch_payload_invalid", "提交展开投递不匹配")
            if delivery.available_at > datetime.now(UTC):
                return
            try:
                with session.begin_nested():
                    done = expand_submission(
                        session,
                        context=context,
                        submission_id=identity,
                        limit=PAGE_SIZE,
                    )
            except DomainError as error:
                # No remote effect; preserve the same repairable generation.
                row.error_code = error.code
                row.repair_after = datetime.now(UTC) + timedelta(seconds=REPAIR_SECONDS)
                session.add(row)
                return
            row.error_code = None
            row.repair_after = datetime.now(UTC) + timedelta(seconds=REPAIR_SECONDS)
            yield_now = not done and (
                page + 1 >= MAX_DELIVERY_PAGES
                or monotonic() >= deadline
                or session.exec(
                    select(PendingDispatch.id)
                    .where(
                        PendingDispatch.tenant_id != tenant_id,
                        # This trusted task constant is the partial-index
                        # predicate, including in PostgreSQL generic plans.
                        PendingDispatch.task_name
                        == literal(TASK_NAME, literal_execute=True),
                        col(PendingDispatch.published_at).is_(None),
                        col(PendingDispatch.superseded_at).is_(None),
                        col(PendingDispatch.retired_at).is_(None),
                        PendingDispatch.available_at <= datetime.now(UTC),
                    )
                    .limit(1)
                ).first()
                is not None
            )
            if yield_now:
                row.dispatch_revision += 1
                queue_expansion(session, row)
            session.add(row)
        if done or yield_now:
            return


def repair_submissions(*, database_engine: Any, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        rows = session.exec(
            select(Submission)
            .where(col(Submission.expanded).is_(False), Submission.repair_after <= now)
            .order_by(col(Submission.repair_after), col(Submission.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            dispatch = session.exec(
                select(PendingDispatch)
                .where(PendingDispatch.id == row.dispatch_id)
                .with_for_update(key_share=True)
            ).one_or_none()
            if dispatch is None:
                queue_expansion(session, row)
            elif (
                dispatch.tenant_id,
                dispatch.actor_id,
                dispatch.task_name,
                dispatch.payload,
            ) != (
                row.tenant_id,
                row.actor_id,
                TASK_NAME,
                {"submission_id": str(row.id), "revision": row.dispatch_revision},
            ):
                row.error_code = "dispatch_payload_invalid"
            elif dispatch.published_at is not None:
                dispatch.published_at, dispatch.available_at = None, now
                session.add(dispatch)
            row.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            session.add(row)
        return len(rows)


@celery_app.task(
    bind=True,
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def expand_submission_task(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    try:
        dispatch_id = UUID(self.request.id)
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "提交展开缺少投递标识") from None
    process_submission(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
        dispatch_id=dispatch_id,
    )


@celery_app.task(name="builds.repair_submissions", time_limit=60, soft_time_limit=55)  # type: ignore[untyped-decorator]
def repair_submission_task() -> int:
    return repair_submissions(database_engine=engine)


def queue_execution_unit(
    session: Session, *, submission: Submission, unit: SubmissionUnit
) -> None:
    """The actual executor is owned by builds.tasks, no placeholder consumer."""
    if unit.disposition != "INCLUDED" or not unit.expanded:
        raise DomainError("dispatch_payload_invalid", "组合步骤尚未展开")
    unit.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=submission.tenant_id,
            actor_id=submission.actor_id,
            role="operator",
        ),
        task_name=UNIT_TASK_NAME,
        task_key=f"build-unit:{unit.unit_id}:{unit.dispatch_revision}",
        payload={"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision},
    )
    unit.due_at = datetime.now(UTC)
    unit.repair_after = unit.due_at + timedelta(seconds=REPAIR_SECONDS)
    session.add(unit)
