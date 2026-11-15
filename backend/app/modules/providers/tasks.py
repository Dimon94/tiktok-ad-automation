"""Bounded provider units, transactional continuations and durable lost-job repair."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from billiard.process import current_process  # type: ignore[import-untyped]
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.providers.link_steps import (
    CLAIM_SECONDS,
    ERROR_MESSAGES,
    _utc,
    has_runnable_link_items,
    run_link_item,
    runnable_link_condition,
)
from app.modules.providers.link_steps import (
    runnable_link_item as _runnable,
)
from app.modules.providers.models import (
    LinkPreparation,
    LinkPreparationItem,
    ProviderSessionRefresh,
)

HARD_LIMIT_SECONDS = 45
WATCHDOG_SECONDS = CLAIM_SECONDS + 5
TASK_NAME = "providers.prepare_item"
register_dispatch_task(TASK_NAME, "resources")


def queue_item(
    session: Session,
    item: LinkPreparationItem,
    *,
    actor_id: UUID,
    revision: int,
    due: datetime | None = None,
) -> UUID:
    due = due or datetime.now(UTC)
    work = dict(item.resolved.get("_work", {}))
    work.update(
        dispatch_revision=revision,
        next_dispatch_at=due.isoformat(),
        repair_after=(due + timedelta(seconds=WATCHDOG_SECONDS)).isoformat(),
    )
    item.resolved = {**item.resolved, "_work": work}
    session.add(item)
    dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=item.tenant_id, actor_id=actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"provider-item:{item.id}:{revision}",
        payload={"item_id": str(item.id), "revision": revision},
    )
    dispatch = session.get(PendingDispatch, dispatch_id)
    assert dispatch is not None
    dispatch.available_at = due
    session.add(dispatch)
    return dispatch_id


def _scoped_item(
    session: Session,
    tenant_id: UUID,
    actor_id: UUID,
    item_id: UUID,
) -> tuple[LinkPreparationItem, LinkPreparation]:
    item = session.exec(
        select(LinkPreparationItem)
        .where(
            LinkPreparationItem.tenant_id == tenant_id,
            LinkPreparationItem.id == item_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if item is None:
        raise DomainError("resource_not_found", "当前租户输入行不存在")
    prep = session.exec(
        select(LinkPreparation).where(
            LinkPreparation.tenant_id == tenant_id,
            LinkPreparation.id == item.preparation_id,
        )
    ).one()
    if prep.actor_id != actor_id:
        raise DomainError("tenant_forbidden", "准备任务操作人不匹配")
    return item, prep


def _quarantine(item: LinkPreparationItem) -> None:
    item.status = "result_unknown"
    item.resolved = {
        **item.resolved,
        "status": "result_unknown",
        "error_code": "provider_state_invalid",
        "error_message": ERROR_MESSAGES["provider_state_invalid"],
        "_work": {"stage": "invalid", "_invalid_work": item.resolved.get("_work")},
    }


def process_item(
    *,
    database_engine: Any,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Testable body. Production caller enforces prefork hard wallclock limit."""
    if (
        set(payload) != {"item_id", "revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "版权方任务参数无效")
    try:
        item_id = UUID(payload["item_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "版权方输入行标识无效") from None
    now = datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        item, prep = _scoped_item(session, tenant_id, actor_id, item_id)
        if not isinstance(item.resolved.get("_work", {}), dict):
            _quarantine(item)
            session.add(item)
            return
        if not _runnable(item):
            return
        work = item.resolved.get("_work", {})
        try:
            revision = work.get("dispatch_revision", 0)
            if type(revision) is not int or revision < 0:
                raise ValueError
            if revision != payload["revision"]:
                return
            if (
                type(work.get("retry_round", 0)) is not int
                or not 0 <= work.get("retry_round", 0) <= 8
            ):
                raise ValueError
            if work.get("claim_until") and _utc(work["claim_until"]) > now:
                return
            if work.get("next_dispatch_at") and _utc(work["next_dispatch_at"]) > now:
                return
        except ValueError, DomainError:
            _quarantine(item)
            session.add(item)
            return
        execution_revision = revision + 1
        # Committed before any provider call; survives worker exit, even when the
        # original delivery has already been acknowledged by the broker.
        queue_item(
            session,
            item,
            actor_id=actor_id,
            revision=execution_revision,
            due=now + timedelta(seconds=WATCHDOG_SECONDS),
        )
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    with Session(database_engine) as session:
        run_link_item(session, context=context, item_id=item_id, transport=transport)
    with Session(database_engine) as session, session.begin():
        item, prep = _scoped_item(session, tenant_id, actor_id, item_id)
        work = dict(item.resolved.get("_work", {}))
        if work.get("dispatch_revision") != execution_revision:
            return
        if _runnable(item):
            retry = (
                item.status in {"retryable_error", "result_unknown"}
                or item.resolved.get("error_code") == "provider_scope_busy"
            )
            rounds = min(work.get("retry_round", 0) + 1, 8) if retry else 0
            work["retry_round"] = rounds
            item.resolved = {**item.resolved, "_work": work}
            delay: float = min(300, 5 * 2**rounds) if retry else 0
            if item.resolved.get("error_code") == "provider_session_refreshing":
                # Connection driver owns its persisted network backoff. Successful
                # login/discovery pages should not exponentially delay each other.
                work["retry_round"] = 0
                item.resolved = {**item.resolved, "_work": work}
                # 登录/目录校验已有进展时立即续步；只有其他领取者或真实网络
                # 退避才等待，不能把每个成功的恢复步骤都额外延后五秒。
                delay = 0
                refresh = session.get(ProviderSessionRefresh, prep.connection_id)
                if (
                    refresh
                    and refresh.tenant_id == tenant_id
                    and refresh.claimed_until
                    and refresh.claimed_until > datetime.now(UTC)
                ):
                    delay = 5
                if refresh and refresh.tenant_id == tenant_id and refresh.due_at:
                    delay = max(
                        delay, (refresh.due_at - datetime.now(UTC)).total_seconds()
                    )
            queue_item(
                session,
                item,
                actor_id=actor_id,
                revision=execution_revision + 1,
                due=datetime.now(UTC) + timedelta(seconds=delay),
            )
        _update_preparation_status(session, prep)


def _update_preparation_status(session: Session, prep: LinkPreparation) -> None:
    session.flush()
    prep = session.exec(
        select(LinkPreparation)
        .where(
            LinkPreparation.tenant_id == prep.tenant_id,
            LinkPreparation.id == prep.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    counts = dict(
        session.exec(
            select(LinkPreparationItem.status, func.count())
            .where(
                LinkPreparationItem.tenant_id == prep.tenant_id,
                LinkPreparationItem.preparation_id == prep.id,
            )
            .group_by(LinkPreparationItem.status)
        ).all()
    )
    total, ready = sum(counts.values()), counts.get("ready", 0)
    if total and ready == total:
        prep.status = "ready"
    elif has_runnable_link_items(
        session, tenant_id=prep.tenant_id, preparation_id=prep.id
    ):
        prep.status = "running"
    else:
        prep.status = "partial_ready" if ready else "failed"
    session.add(prep)


def recover_preparations(*, database_engine: Any, limit: int = 100) -> int:
    """Fair bounded repair: oldest due rows first, then move repaired rows forward."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Recovery batch must be between 1 and 100")
    now = datetime.now(UTC)
    next_at = func.jsonb_extract_path_text(
        col(LinkPreparationItem.resolved), "_work", "next_dispatch_at"
    )
    repair_at = func.jsonb_extract_path_text(
        col(LinkPreparationItem.resolved), "_work", "repair_after"
    )
    due_at = func.coalesce(repair_at, next_at)
    canonical_date = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00$"
    repaired = 0
    with Session(database_engine) as session, session.begin():
        rows = session.exec(
            select(LinkPreparationItem)
            .where(
                runnable_link_condition(),
                or_(
                    due_at.is_(None),
                    due_at <= now.isoformat(),
                    ~due_at.op("~")(canonical_date),
                ),
            )
            .order_by(due_at.asc().nulls_first(), col(LinkPreparationItem.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for item in rows:
            raw = item.resolved.get("_work", {})
            try:
                if not isinstance(raw, dict):
                    raise ValueError
                revision = raw.get("dispatch_revision", 0)
                if type(revision) is not int or revision < 0:
                    raise ValueError
                if raw.get("next_dispatch_at"):
                    _utc(raw["next_dispatch_at"])
                if raw.get("repair_after"):
                    _utc(raw["repair_after"])
                if raw.get("claim_until") and _utc(raw["claim_until"]) > now:
                    # Valid active work is not rewritten beneath its claim token.
                    continue
            except ValueError, DomainError:
                _quarantine(item)
                session.add(item)
                continue
            prep = session.exec(
                select(LinkPreparation).where(
                    LinkPreparation.tenant_id == item.tenant_id,
                    LinkPreparation.id == item.preparation_id,
                )
            ).one()
            dispatch = session.exec(
                select(PendingDispatch)
                .where(
                    PendingDispatch.tenant_id == item.tenant_id,
                    PendingDispatch.task_key == f"provider-item:{item.id}:{revision}",
                )
                .with_for_update()
            ).one_or_none()
            if dispatch is None:
                queue_item(
                    session, item, actor_id=prep.actor_id, revision=revision, due=now
                )
            else:
                if (
                    dispatch.task_name != TASK_NAME
                    or dispatch.actor_id != prep.actor_id
                    or dispatch.payload
                    != {"item_id": str(item.id), "revision": revision}
                ):
                    _quarantine(item)
                    session.add(item)
                    continue
                # An unpublished delivery is healthy outbox backlog (possibly
                # under broker backoff). A published delivery may be lost OR
                # merely queued at a slow broker. Re-arm the same identity so
                # the original and duplicate stay executable until a worker
                # claims the revision. Only actual execution advances revision.
                if dispatch.published_at is not None:
                    dispatch.published_at = None
                    dispatch.available_at = now
                    session.add(dispatch)
                work = dict(raw)
                work["repair_after"] = (
                    now + timedelta(seconds=WATCHDOG_SECONDS)
                ).isoformat()
                item.resolved = {**item.resolved, "_work": work}
                session.add(item)
            repaired += 1
    return repaired


@celery_app.task(  # type: ignore[untyped-decorator]
    name=TASK_NAME,
    bind=True,
    time_limit=HARD_LIMIT_SECONDS,
    soft_time_limit=40,
    acks_late=True,
    reject_on_worker_lost=True,
)
def prepare_item(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    limits = self.request.timelimit or (None, None)
    hard_limit = limits[0] or self.time_limit
    if (
        not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or self.request.called_directly
        or self.request.is_eager
        or isinstance(hard_limit, bool)
        or not isinstance(hard_limit, (int, float))
        or not 0 < hard_limit <= HARD_LIMIT_SECONDS
    ):
        raise DomainError(
            "provider_worker_unbounded", "取链需要启用 prefork 硬超时工作进程"
        )
    process_item(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
    )


@celery_app.task(name="providers.recover_preparations")  # type: ignore[untyped-decorator]
def recover_preparations_task() -> int:
    return recover_preparations(database_engine=engine)
