"""Local-only preparation; no external calls occur while holding draft locks."""

from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.builds.models import BuildDraft, DraftDrama, DraftPreparation

TASK_NAME = "builds.prepare_draft"
register_dispatch_task(TASK_NAME, "builds")
REPAIR_SECONDS = 120
MATERIAL_PAGES_PER_TURN = 5
MATERIAL_TURN_SECONDS = 1.0


def _has_pending_materials(session: Session, prep: DraftPreparation) -> bool:
    return (
        session.exec(
            select(DraftDrama.drama_id)
            .where(
                DraftDrama.tenant_id == prep.tenant_id,
                DraftDrama.draft_id == prep.draft_id,
                col(DraftDrama.material_state).in_(["pending", "matching"]),
            )
            .limit(1)
        ).first()
        is not None
    )


def queue_preparation(session: Session, prep: DraftPreparation, *, delay: int) -> None:
    prep.due_at = datetime.now(UTC) + timedelta(seconds=delay)
    prep.repair_after = prep.due_at + timedelta(seconds=REPAIR_SECONDS)
    prep.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=prep.tenant_id, actor_id=prep.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"draft-preparation:{prep.id}:{prep.generation}",
        payload={"preparation_id": str(prep.id), "generation": prep.generation},
    )
    dispatch = session.get(PendingDispatch, prep.dispatch_id)
    assert dispatch
    dispatch.available_at = prep.due_at
    session.add_all([prep, dispatch])


def process_preparation(
    *, database_engine: Any, tenant_id: UUID, actor_id: UUID, payload: dict[str, Any]
) -> None:
    from app.modules.builds.drafts import continue_draft

    if (
        set(payload) != {"preparation_id", "generation"}
        or type(payload["generation"]) is not int
        or payload["generation"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "草稿准备任务参数无效")
    try:
        identity = UUID(payload["preparation_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "草稿准备任务标识无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    with Session(database_engine) as session, session.begin():
        prep = session.exec(
            select(DraftPreparation).where(
                DraftPreparation.tenant_id == tenant_id, DraftPreparation.id == identity
            )
        ).one_or_none()
        if prep is None or prep.actor_id != actor_id:
            raise DomainError("draft_not_found", "草稿准备任务不存在")
        # Every path locks parent then child; delivery duplication cannot reverse
        # the order used by editing, prepare requests, and the local consumer.
        draft = session.exec(
            select(BuildDraft)
            .where(BuildDraft.tenant_id == tenant_id, BuildDraft.id == prep.draft_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        prep = session.exec(
            select(DraftPreparation)
            .where(DraftPreparation.id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if (
            prep.status != "PENDING"
            or prep.generation != payload["generation"]
            or prep.due_at > datetime.now(UTC)
        ):
            return
        if prep.draft_revision != draft.revision:
            prep.status = "OBSOLETE"
            if draft.media_kind == "CAROUSEL":
                from .image_candidate_service import fail_initial_candidate_snapshot

                fail_initial_candidate_snapshot(
                    session, prep, "draft_revision_conflict"
                )
            session.add(prep)
            return
        try:
            with session.begin_nested():
                started = monotonic()
                previous_phase, previous_account_after = prep.phase, prep.account_after
                material_progress = False
                pending_materials = (
                    prep.phase == "materials" and _has_pending_materials(session, prep)
                )
                # 仅批处理本地选材；每轮最多五页或一秒，保留断点并及时释放草稿锁。
                # 单页不可中断，时间预算只决定是否继续下一页。
                for _ in range(MATERIAL_PAGES_PER_TURN):
                    material_progress = material_progress or pending_materials
                    done = continue_draft(session, context=context, task_id=identity)
                    pending_materials = (
                        not done
                        and prep.phase == "materials"
                        and _has_pending_materials(session, prep)
                    )
                    if (
                        not pending_materials
                        or monotonic() - started >= MATERIAL_TURN_SECONDS
                    ):
                        break
        except DomainError as error:
            if draft.media_kind == "CAROUSEL":
                from .image_candidate_service import fail_initial_candidate_snapshot

                fail_initial_candidate_snapshot(session, prep, error.code)
            prep.status, prep.error_code, draft.status = (
                "BLOCKED",
                error.code,
                "BLOCKED",
            )
            session.add_all([prep, draft])
            return
        if not done:
            # 本地账户分页、账户完成以及已得到链接的阶段交接不需要五秒退避。
            # 链接仍在远端准备且没有新素材时，保留原等待，避免 links/materials 空转。
            local_progress = (
                prep.account_after != previous_account_after
                or previous_phase == "accounts"
                and prep.phase != "accounts"
                or previous_phase == "links"
                and prep.phase == "materials"
                and (not prep.pending_links or _has_pending_materials(session, prep))
            )
            prep.generation += 1
            queue_preparation(
                session,
                prep,
                delay=5
                if not local_progress
                and (
                    prep.phase == "accounts"
                    or prep.phase == "materials"
                    and not (material_progress or pending_materials)
                    or prep.phase == "scenes"
                    or prep.phase == "links"
                    and prep.link_cursor is None
                )
                else 0,
            )


def repair_preparations(*, database_engine: Any, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair batch must be between 1 and 100")
    now = datetime.now(UTC)
    count = 0
    with Session(database_engine) as session, session.begin():
        rows = session.exec(
            select(DraftPreparation)
            .where(
                DraftPreparation.status == "PENDING",
                DraftPreparation.repair_after <= now,
            )
            .order_by(col(DraftPreparation.repair_after), col(DraftPreparation.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for prep in rows:
            dispatch = session.exec(
                select(PendingDispatch)
                .where(PendingDispatch.id == prep.dispatch_id)
                .with_for_update()
            ).one_or_none()
            if dispatch is None:
                queue_preparation(session, prep, delay=0)
            elif (
                dispatch.tenant_id != prep.tenant_id
                or dispatch.actor_id != prep.actor_id
                or dispatch.task_name != TASK_NAME
                or dispatch.payload
                != {"preparation_id": str(prep.id), "generation": prep.generation}
            ):
                prep.status, prep.error_code = "BLOCKED", "dispatch_payload_invalid"
            elif dispatch.published_at is not None:
                # Reuse transport identity: a merely delayed original stays
                # executable; an unpublished delivery retains broker backoff.
                dispatch.published_at, dispatch.available_at = None, now
                session.add(dispatch)
            prep.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            session.add(prep)
            count += 1
    return count


@celery_app.task(  # type: ignore[untyped-decorator]
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def prepare_draft_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    process_preparation(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
    )


@celery_app.task(name="builds.repair_draft_preparations")  # type: ignore[untyped-decorator]
def repair_draft_preparations_task() -> int:
    return repair_preparations(database_engine=engine)
