"""Scoped task queries and human recovery actions for ad management."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import exists, func, literal, tuple_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page, count_rows
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.outbox import enqueue_after_commit
from app.models import User
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.execution import _public as _item_public
from app.modules.ad_management.history_scope import readable_history
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.reconciliation import reconcile_item
from app.modules.ad_management.schemas import (
    ManagementAttemptPublic,
    ManagementCounts,
    ManagementItemPublic,
    ManagementPreviewPublic,
    ManagementTaskMutation,
    ManagementTaskPublic,
    ManagementTaskSource,
)
from app.modules.ad_management.submissions import (
    MANAGEMENT_DISPATCH_TASK,
    _idempotency_lock,
)
from app.modules.ads.models import AdObject
from app.modules.automation_rules.models import AutomationActionLedger, AutomationRule
from app.modules.tenants.permissions import require_tenant


def _public(
    task: ManagementTask,
    *,
    items: tuple[ManagementItemPublic, ...] = (),
) -> ManagementTaskPublic:
    return ManagementTaskPublic(
        task_id=task.id,
        bc_id=task.bc_id,
        status=cast(Any, task.status),
        counts=ManagementCounts.model_validate(task.counts or {}),
        items=items,
    )


def _route(task: ManagementTask) -> FrozenTikTokRoute:
    try:
        return FrozenTikTokRoute.model_validate(task.route)
    except (TypeError, ValueError) as exc:
        raise DomainError("frozen_route_changed", "任务冻结路由无效") from exc


def _scoped_task(
    session: Session,
    context: TenantContext,
    bc_id: str,
    task_id: UUID,
    *,
    action: str = "read",
) -> ManagementTask:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action=action,
    )
    task = session.exec(
        select(ManagementTask).where(
            col(ManagementTask.id) == task_id,
            col(ManagementTask.tenant_id) == context.tenant_id,
            col(ManagementTask.bc_id) == bc_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    # 历史权限按集合核验，万条任务不逐项重复查询目录/授权。
    visible = session.exec(
        select(col(ManagementTask.id)).where(
            col(ManagementTask.id) == task.id, readable_history(context, bc_id)
        )
    ).first()
    if visible is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    if action == "ads_manage":
        route = _route(task)
        scopes = session.exec(
            select(
                col(ManagementTaskItem.ref)["advertiser_id"].astext,
                col(ManagementTaskItem.capability)["operation"].astext,
                col(ManagementTaskItem.ref)["kind"].astext,
            )
            .where(
                col(ManagementTaskItem.tenant_id) == context.tenant_id,
                col(ManagementTaskItem.task_id) == task.id,
                col(ManagementTaskItem.execution_result).in_(
                    ["PENDING", "NOT_SENT", "ACCEPTED"]
                ),
            )
            .distinct()
        ).all()
        for advertiser_id, operation, entity_kind in scopes:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser_id,
                capability="ads_manage",
                operation=operation,
                entity_kind=entity_kind,
            )
    return task


def get_task(
    session: Session,
    context: TenantContext,
    bc_id: str,
    task_id: UUID,
    *,
    offset: int = 0,
    limit: int = 100,
) -> ManagementTaskPublic:
    if offset < 0 or not 1 <= limit <= 100:
        raise DomainError("invalid_cursor", "分页大小无效")
    task = _scoped_task(session, context, bc_id, task_id)
    rows = session.exec(
        select(ManagementTaskItem)
        .where(
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
        )
        .order_by(col(ManagementTaskItem.position))
        .offset(offset)
        .limit(limit)
    ).all()
    by_item: dict[UUID, list[ManagementRequestAttempt]] = {}
    if rows:
        for attempt in session.exec(
            select(ManagementRequestAttempt)
            .where(
                col(ManagementRequestAttempt.tenant_id) == context.tenant_id,
                col(ManagementRequestAttempt.task_item_id).in_(
                    [row.id for row in rows]
                ),
            )
            .order_by(col(ManagementRequestAttempt.attempt))
        ).all():
            by_item.setdefault(attempt.task_item_id, []).append(attempt)
    public_items: list[ManagementItemPublic] = []
    for row in rows:
        attempts = by_item.get(row.id, [])
        public_items.append(
            _item_public(row).model_copy(
                update={
                    "attempts": tuple(
                        ManagementAttemptPublic(
                            attempt=attempt.attempt,
                            request_at=attempt.request_at,
                            outcome=cast(Any, attempt.outcome),
                            request_id=attempt.request_id,
                            retryable=attempt.retryable,
                        )
                        for attempt in attempts
                    )
                }
            )
        )
    summary = _with_summaries(session, context, [task])[0]
    total = session.exec(
        select(func.count())
        .select_from(ManagementTaskItem)
        .where(
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
            col(ManagementTaskItem.task_id) == task.id,
        )
    ).one()
    return summary.model_copy(
        update={
            "items": tuple(public_items),
            "items_total": total,
            "items_offset": offset,
            "next_items_offset": offset + limit if offset + limit < total else None,
            "preparation_error": task.request.get("preparation_error_message")
            or (
                "后台准备失败，请检查当前权限与连接后重试。"
                if task.request.get("preparation_error")
                else None
            ),
            "preparation_problems": task.request.get("row_problems", []),
        }
    )


# 操作记录筛选：动作按冻结的变更字段归类，启停再按目标状态拆成启用/暂停。
TaskActionFilter = Literal["enable", "disable", "budget", "roas", "rename"]
TaskSourceFilter = Literal["manual", "rule"]


def _encode_cursor(task: ManagementTask) -> str:
    return f"{task.created_at.isoformat()}|{task.id}"


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        created_at, task_id = cursor.split("|", 1)
        return datetime.fromisoformat(created_at), UUID(task_id)
    except ValueError as exc:
        raise DomainError("invalid_cursor", "分页游标无效") from exc


def _with_summaries(
    session: Session, context: TenantContext, tasks: list[ManagementTask]
) -> list[ManagementTaskPublic]:
    """批量补齐操作记录要展示的时间、操作人、动作、来源和对象层级。

    自动规则以规则最后修改人的身份提交任务，只看操作人分不出人工还是规则，
    所以来源以规则动作台账里登记的任务 ID 为准。
    """
    if not tasks:
        return []
    task_ids = [task.id for task in tasks]
    mutations = dict(
        session.exec(
            select(col(ManagementPreview.id), col(ManagementPreview.mutation)).where(
                col(ManagementPreview.tenant_id) == context.tenant_id,
                col(ManagementPreview.id).in_([task.preview_id for task in tasks]),
            )
        ).all()
    )
    actors = {
        user.id: user.full_name or user.username
        for user in session.exec(
            select(User).where(col(User.id).in_({task.actor_id for task in tasks}))
        ).all()
    }
    sources: dict[UUID, ManagementTaskSource] = {}
    for task_id, rule_id, rule_name in session.exec(
        select(
            col(AutomationActionLedger.management_task_id),
            col(AutomationActionLedger.rule_id),
            col(AutomationRule.name),
        )
        .join(
            AutomationRule,
            col(AutomationRule.id) == col(AutomationActionLedger.rule_id),
            isouter=True,
        )
        .where(
            col(AutomationActionLedger.tenant_id) == context.tenant_id,
            col(AutomationActionLedger.management_task_id).in_(task_ids),
        )
        .distinct()
    ).all():
        if task_id is not None:
            sources[task_id] = ManagementTaskSource(
                kind="rule", rule_id=rule_id, rule_name=rule_name
            )
    kind = col(ManagementTaskItem.ref)["kind"].astext
    target_kinds: dict[UUID, dict[str, int]] = {}
    for task_id, entity_kind, total in session.exec(
        select(col(ManagementTaskItem.task_id), kind, func.count())
        .where(
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
            col(ManagementTaskItem.task_id).in_(task_ids),
        )
        .group_by(col(ManagementTaskItem.task_id), kind)
    ).all():
        target_kinds.setdefault(task_id, {})[str(entity_kind)] = int(total)
    result: list[ManagementTaskPublic] = []
    for task in tasks:
        raw = (
            task.request.get("mutation", {})
            if task.source_kind == "DIRECT"
            else mutations.get(task.preview_id) or {}
        )
        try:
            mutation = ManagementTaskMutation(
                field=raw["field"],
                mode=raw["mode"],
                value=raw.get("value"),
                rename_kind=(raw.get("rename") or {}).get("kind"),
            )
        except KeyError, TypeError, ValueError:
            mutation = None
        result.append(
            _public(task).model_copy(
                update={
                    "created_at": task.created_at,
                    "actor_name": actors.get(task.actor_id),
                    "mutation": mutation,
                    "source": sources.get(task.id)
                    or ManagementTaskSource(kind="manual"),
                    "target_kinds": target_kinds.get(task.id, {}),
                }
            )
        )
    return result


def list_tasks(
    session: Session,
    context: TenantContext,
    bc_id: str,
    *,
    cursor: str | None,
    limit: int,
    action: TaskActionFilter | None = None,
    source: TaskSourceFilter | None = None,
) -> Page[ManagementTaskPublic]:
    if not 1 <= limit <= 100:
        raise DomainError("invalid_cursor", "分页大小无效")
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="read",
    )
    statement = select(ManagementTask).where(
        col(ManagementTask.tenant_id) == context.tenant_id,
        col(ManagementTask.bc_id) == bc_id,
        readable_history(context, bc_id),
    )
    if action is not None:
        field = func.coalesce(
            col(ManagementTask.request)["mutation"]["field"].astext,
            col(ManagementPreview.mutation)["field"].astext,
        )
        value = func.coalesce(
            col(ManagementTask.request)["mutation"]["value"].astext,
            col(ManagementPreview.mutation)["value"].astext,
        )
        statement = statement.join(
            ManagementPreview,
            (col(ManagementPreview.tenant_id) == col(ManagementTask.tenant_id))
            & (col(ManagementPreview.id) == col(ManagementTask.preview_id)),
            isouter=True,
        )
        if action in {"enable", "disable"}:
            statement = statement.where(field == "status", value == action.upper())
        elif action == "rename":
            # 改名动作冻结的变更字段是 name。
            statement = statement.where(field == "name")
        else:
            statement = statement.where(field == action)
    if source is not None:
        from_rule = exists().where(
            col(AutomationActionLedger.tenant_id) == context.tenant_id,
            col(AutomationActionLedger.management_task_id) == col(ManagementTask.id),
        )
        statement = statement.where(from_rule if source == "rule" else ~from_rule)
    # 操作记录按提交时间倒序；同一时刻用任务 ID 稳定排序，游标同时携带两者。
    order = (col(ManagementTask.created_at).desc(), col(ManagementTask.id).desc())
    after = _decode_cursor(cursor) if cursor else None
    # 在完整权限/筛选范围内 COUNT，游标只作用于当前页，避免后续页总数递减。
    total = count_rows(session, statement)
    if after is not None:
        statement = statement.where(
            tuple_(col(ManagementTask.created_at), col(ManagementTask.id))
            < tuple_(literal(after[0]), literal(after[1]))
        )
    # 数据库最多返回当前页加一条探测记录，不再遍历历史或逐项读取授权。
    rows = session.exec(statement.order_by(*order).limit(limit + 1)).all()
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return Page(
        items=_with_summaries(session, context, list(rows[:limit])),
        next_cursor=next_cursor,
        total=total,
    )


def cancel_task(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementTaskPublic:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="ads_manage",
    )
    task = session.exec(
        select(ManagementTask)
        .where(
            col(ManagementTask.id) == task_id,
            col(ManagementTask.tenant_id) == context.tenant_id,
        )
        .with_for_update()
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="ads_manage")
    if task.status in {"CANCELLED", "SUCCEEDED", "FAILED", "PARTIAL", "NEEDS_REVIEW"}:
        raise DomainError("management_cancel_not_allowed", "任务当前不可取消")
    rows = session.exec(
        select(ManagementTaskItem)
        .where(
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
        )
        .with_for_update()
    ).all()
    for item in rows:
        if (
            item.execution_result in {"PENDING", "NOT_SENT"}
            and item.claim_token is None
        ):
            item.execution_result = "CANCELLED"
            item.delivery_status = "CANCELLED"
            item.reason = "cancelled"
            session.add(item)
    task.status = "CANCELLED"
    session.add(task)
    session.flush()
    return _public(task)


def retry_task(
    session: Session,
    context: TenantContext,
    task_id: UUID,
    idempotency_key: UUID,
) -> ManagementTaskPublic:
    if not isinstance(idempotency_key, UUID):
        raise DomainError("management_retry_invalid", "重试需要幂等键")
    source = session.exec(
        select(ManagementTask).where(
            col(ManagementTask.id) == task_id,
            col(ManagementTask.tenant_id) == context.tenant_id,
        )
    ).one_or_none()
    if source is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, source.bc_id, source.id, action="ads_manage")
    _idempotency_lock(session, context, idempotency_key)
    existing = session.exec(
        select(ManagementTask).where(
            col(ManagementTask.tenant_id) == context.tenant_id,
            col(ManagementTask.actor_id) == context.actor_id,
            col(ManagementTask.idempotency_key) == idempotency_key,
        )
    ).one_or_none()
    if existing is not None:
        if existing.digest != source.digest or existing.preview_id != source.preview_id:
            raise DomainError("idempotency_conflict", "幂等键已绑定其他任务")
        return _public(existing)
    items = session.exec(
        select(ManagementTaskItem)
        .where(
            col(ManagementTaskItem.task_id) == source.id,
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
        )
        .order_by(col(ManagementTaskItem.position))
    ).all()
    eligible: list[ManagementTaskItem] = []
    for item in items:
        if item.execution_result != "NOT_SENT" or item.delivery_status == "CANCELLED":
            continue
        latest = session.exec(
            select(ManagementRequestAttempt)
            .where(
                col(ManagementRequestAttempt.tenant_id) == item.tenant_id,
                col(ManagementRequestAttempt.task_item_id) == item.id,
            )
            .order_by(col(ManagementRequestAttempt.attempt).desc())
        ).first()
        if latest is None or latest.retryable:
            eligible.append(item)
    resume_preparation = (
        source.source_kind == "DIRECT"
        and source.status == "FAILED"
        and bool(source.request.get("preparation_error"))
    )
    if resume_preparation:
        eligible = list(items)
    if not eligible and not resume_preparation:
        raise DomainError(
            "management_retry_not_allowed", "没有明确未发送的任务项可重试"
        )
    task = ManagementTask(
        tenant_id=source.tenant_id,
        bc_id=source.bc_id,
        actor_id=context.actor_id,
        preview_id=source.preview_id,
        source_kind=source.source_kind,
        request={
            key: value
            for key, value in source.request.items()
            if key not in {"preparation_error", "preparation_error_message"}
        },
        prepare_cursor=source.prepare_cursor,
        idempotency_key=idempotency_key,
        digest=source.digest,
        route=dict(source.route),
        status="PREPARING" if resume_preparation else "QUEUED",
        counts=dict(source.counts)
        if resume_preparation
        else ManagementCounts(
            selected=len(eligible),
            targets=len(eligible),
            pending=len(eligible),
            prepared=len(eligible),
        ).model_dump(),
    )
    session.add(task)
    session.flush()
    for position, old in enumerate(eligible):
        session.add(
            ManagementTaskItem(
                tenant_id=old.tenant_id,
                task_id=task.id,
                source_kind=old.source_kind,
                preview_item_id=old.preview_item_id,
                preview_id=old.preview_id,
                position=position,
                ref=dict(old.ref),
                material_use=dict(old.material_use) if old.material_use else None,
                original_value=old.original_value,
                final_value=old.final_value,
                reason=None,
                grouping_revision=old.grouping_revision,
                membership_digest=old.membership_digest,
                execution_result=old.execution_result
                if resume_preparation
                else "PENDING",
                capability=dict(old.capability),
                parent_ref=dict(old.parent_ref) if old.parent_ref else None,
            )
        )
    enqueue_after_commit(
        session,
        context=context,
        task_name=MANAGEMENT_DISPATCH_TASK,
        task_key=f"ad-management:{task.id}",
        payload={"task_id": str(task.id), "generation": 1},
    )
    if resume_preparation:
        from app.jobs.outbox import enqueue_many_after_commit
        from app.modules.ad_management.preparation_dispatch import (
            preparation_watchdog_entry,
        )

        enqueue_many_after_commit(
            session, context=context, entries=[preparation_watchdog_entry(task)]
        )
    session.flush()
    return _public(task)


def prepare_restore(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementPreviewPublic:
    task = session.exec(
        select(ManagementTask).where(
            col(ManagementTask.id) == task_id,
            col(ManagementTask.tenant_id) == context.tenant_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="ads_manage")
    old_items = session.exec(
        select(ManagementTaskItem)
        .where(
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
        )
        .order_by(col(ManagementTaskItem.position))
    ).all()
    source_preview = (
        session.get(ManagementPreview, task.preview_id) if task.preview_id else None
    )
    source_mutation = (
        task.request.get("mutation", {})
        if task.source_kind == "DIRECT"
        else (source_preview.mutation if source_preview else {})
    )
    if source_preview is None and task.source_kind != "DIRECT":
        raise DomainError("management_preview_not_found", "原管理预览不存在")
    if source_mutation.get("field") == "name":
        # 改名的原名可能已被其它改名占用，一键回滚无法保证不重名，需人工重新提交。
        raise DomainError(
            "management_restore_unsupported",
            "改名任务不支持一键恢复，需要时请再提交一次改名",
        )
    now = datetime.now(UTC)
    route = _route(task)
    digest_payload = {
        "task": str(task.id),
        "route": route.model_dump(mode="json"),
        "restore": True,
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True).encode()
    ).hexdigest()
    if source_preview is None:
        from app.modules.reporting.selection import create_target_selection

        restore_selection = create_target_selection(
            session,
            context=context,
            bc_id=task.bc_id,
            refs=[i.ref for i in old_items],
            uses=[i.material_use for i in old_items if i.material_use],
            filters={"source_task_id": str(task.id), "purpose": "restore"},
            now=now,
        )
        selection_id = restore_selection.id
    else:
        selection_id = source_preview.selection_id
    preview = ManagementPreview(
        tenant_id=task.tenant_id,
        bc_id=task.bc_id,
        actor_id=context.actor_id,
        selection_id=selection_id,
        digest=digest,
        mutation={"field": "restore", "source_task_id": str(task.id)},
        route=route.model_dump(mode="json"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        status="READY",
        counts={
            "selected": len(old_items),
            "targets": len(old_items),
            "linked": 0,
            "unsupported": 0,
        },
    )
    session.add(preview)
    session.flush()
    public_items: list[ManagementItemPublic] = []
    for position, old in enumerate(old_items):
        conflict = False
        already_disabled = False
        ref = old.ref
        current = session.get(
            AdObject,
            (
                old.tenant_id,
                str(ref.get("advertiser_id")),
                str(ref.get("kind")),
                str(ref.get("remote_id")),
            ),
        )
        operation = str((old.capability or {}).get("operation"))
        if current is None:
            conflict = True
        elif operation == "set_status":
            status = current.operation_status
            if status is None:
                conflict = True
            elif status == "DISABLE":
                already_disabled = True
            elif str(status) != str(old.final_value):
                conflict = True
        else:
            key = "roas_bid" if operation == "update_roas" else "budget"
            value = (current.configuration or {}).get(key)
            if value is None or str(value) != str(old.final_value):
                conflict = True
        reason = (
            "configuration_conflict"
            if conflict
            else ("already_disabled" if already_disabled else None)
        )
        execution_result = (
            "CONFLICT" if conflict else ("NO_CHANGE" if already_disabled else "PENDING")
        )
        session.add(
            ManagementPreviewItem(
                tenant_id=old.tenant_id,
                preview_id=preview.id,
                position=position,
                ref=dict(old.ref),
                original_value=old.final_value,
                final_value=old.original_value,
                reason=reason,
                execution_result=execution_result,
                parent_ref=dict(old.parent_ref) if old.parent_ref else None,
                capability=dict(old.capability),
            )
        )
        public_items.append(
            ManagementItemPublic(
                ref=old.ref,
                original_value=None,
                final_value=old.original_value,
                reason=reason,
                execution_result=cast(Any, execution_result),
            )
        )
    session.flush()
    return ManagementPreviewPublic(
        preview_id=preview.id,
        digest=digest,
        created_at=now,
        expires_at=preview.expires_at,
        bc_id=task.bc_id,
        route=route,
        items=tuple(public_items),
        counts=ManagementCounts.model_validate(preview.counts),
    )


def reconcile(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementTaskPublic:
    task = session.exec(
        select(ManagementTask).where(
            col(ManagementTask.id) == task_id,
            col(ManagementTask.tenant_id) == context.tenant_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="read")
    rows = session.exec(
        select(ManagementTaskItem).where(
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
        )
    ).all()
    # Reconciliation is deliberately local-only; UNKNOWN and ACCEPTED remain
    # terminal and no provider transport is opened.
    for row in rows:
        if row.execution_result == "UNKNOWN" and task.actor_id == context.actor_id:
            reconcile_item(session, context, row.id)
    return _public(task)


def retry_targeted_refresh(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementTaskPublic:
    """Queue only durable post-write refreshes; never replay a mutation."""
    task = session.exec(
        select(ManagementTask)
        .where(
            col(ManagementTask.id) == task_id,
            col(ManagementTask.tenant_id) == context.tenant_id,
        )
        .with_for_update()
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="ads_manage")
    pending = session.exec(
        select(col(ManagementTaskItem.id)).where(
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
            col(ManagementTaskItem.execution_result) == "ACCEPTED",
            col(ManagementTaskItem.observation_state) == "REFRESH_PENDING",
        )
    ).all()
    if not pending:
        raise DomainError(
            "management_refresh_retry_not_allowed", "没有待重试的定向刷新"
        )
    enqueue_after_commit(
        session,
        context=context,
        task_name=MANAGEMENT_DISPATCH_TASK,
        task_key=f"ad_management.refresh.manual:{task.id}:{uuid4()}",
        payload={"task_id": str(task.id), "generation": 1},
    )
    task.status = "RUNNING"
    session.add(task)
    session.flush()
    return _public(task)


__all__ = [
    "cancel_task",
    "get_task",
    "list_tasks",
    "prepare_restore",
    "reconcile",
    "retry_targeted_refresh",
    "retry_task",
]
