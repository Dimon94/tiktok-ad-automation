"""Execute one frozen advertising-management item.

The executor is deliberately item scoped.  A provider request is preceded by a
committed attempt row and fenced by both the persisted claim and the shared
Redis object lease.  A response with unknown effect is terminal until a human
reconciles it; this module never guesses that a request was not sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import Engine, func
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.integrations.tiktok.contracts.common import TRANSIENT_NOT_SENT, RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.management import (
    ManagementCommand,
    ManagementField,
    ManagementReceipt,
    field_for_operation,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.object_coordination import MutationLease, claim_mutation
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.models import (
    ManagementReceipt as ReceiptRow,
)
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.schemas import ManagementItemPublic
from app.modules.ads.directory import current_ad_materials, locate_material_use
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.models import AdObject
from app.modules.reporting.planner import plan_hot
from app.modules.reporting.store_models import AdSyncBc

_CLAIM_SECONDS = 120
_TERMINAL = frozenset(
    {
        "ACCEPTED",
        "REJECTED",
        "UNKNOWN",
        "NO_CHANGE",
        "CONFLICT",
        "UNSUPPORTED",
        "CANCELLED",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _redis() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _ref(value: dict[str, Any]) -> EntityRef:
    try:
        return EntityRef(
            UUID(str(value["tenant_id"])),
            str(value["advertiser_id"]),
            value["kind"],
            str(value["remote_id"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DomainError("management_request_invalid", "冻结目标身份无效") from exc


def _material(value: dict[str, Any] | None) -> MaterialUseRef | None:
    if value is None:
        return None
    try:
        return MaterialUseRef(
            _ref(value["ad_ref"]),
            str(value["platform_material_id"]),
            value.get("ad_material_id"),
            str(value["material_type"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DomainError("management_request_invalid", "冻结素材身份无效") from exc


def _command(item: ManagementTaskItem) -> ManagementCommand:
    ref = _ref(item.ref)
    capability = item.capability or {}
    operation = capability.get("operation")
    field = field_for_operation(str(operation))
    if field is None:
        raise DomainError("management_operation_invalid", "冻结管理操作无效")
    original = dict(capability.get("configuration") or {})
    if item.parent_ref and item.parent_ref.get("kind") == "campaign":
        original["campaign_id"] = item.parent_ref["remote_id"]
    if item.material_use and item.material_use.get("material_type"):
        original["material_type"] = item.material_use["material_type"]
    if capability.get("ad_type") is not None:
        original.setdefault("ad_type", capability["ad_type"])
    final = item.final_value
    if final is None:
        raise DomainError("management_request_invalid", "冻结最终值缺失")
    desired: dict[str, Any]
    if field == "roas":
        desired = {"roas_bid": final}
    elif field == "budget":
        desired = {"budget": final}
    elif field == "name":
        desired = {"campaign_name": final}
    else:
        desired = {"operation_status": final}
    return ManagementCommand(
        ref=ref,
        field=cast(ManagementField, field),
        original=original,
        desired=desired,
        ad_material_id=capability.get("ad_material_id"),
    )


def _public(item: ManagementTaskItem) -> ManagementItemPublic:
    def decimal(value: str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation, ValueError:
            return None

    original: Decimal | str | None
    final: Decimal | str | None = item.final_value
    if (item.capability or {}).get("operation") == "rename":
        # 改名的原值与最终值都是系列名称，原样展示；纯数字名称也不能被转成数值。
        original = item.original_value
    else:
        original = decimal(item.original_value)
        if final is not None and str(final) not in {"ENABLE", "DISABLE"}:
            try:
                final = Decimal(str(final))
            except InvalidOperation, ValueError:
                pass
    return ManagementItemPublic(
        ref=_ref(item.ref),
        material_use=_material(item.material_use),
        original_value=original,
        final_value=final,
        reason=item.reason,
        execution_result=cast(Any, item.execution_result),
        observation_state=item.observation_state,
        delivery_status=item.delivery_status,
        request_attribution=item.request_attribution,
    )


def _route(task: ManagementTask) -> FrozenTikTokRoute:
    try:
        return FrozenTikTokRoute.model_validate(task.route)
    except (TypeError, ValueError) as exc:
        raise DomainError("frozen_route_scope_mismatch", "冻结执行路由无效") from exc


def _attempts(
    session: Session, item: ManagementTaskItem
) -> list[ManagementRequestAttempt]:
    return list(
        session.exec(
            select(ManagementRequestAttempt)
            .where(
                ManagementRequestAttempt.tenant_id == item.tenant_id,
                ManagementRequestAttempt.task_item_id == item.id,
            )
            .order_by(col(ManagementRequestAttempt.attempt))
        ).all()
    )


def _can_run(
    session: Session,
    item: ManagementTaskItem,
    attempts: list[ManagementRequestAttempt],
) -> bool:
    if item.execution_result in _TERMINAL:
        return False
    if any(row.outcome == "UNKNOWN" for row in attempts):
        item.execution_result = "UNKNOWN"
        item.observation_state = "NEEDS_REVIEW"
        return False
    latest = attempts[-1] if attempts else None
    if latest is not None and latest.outcome == "ACCEPTED":
        item.execution_result = "ACCEPTED"
        return False
    if (
        latest is not None
        and latest.outcome in {"NOT_SENT", "REJECTED"}
        and not latest.retryable
    ):
        if latest.outcome == "NOT_SENT" and item.delivery_status == "ATTEMPT_RECORDED":
            # While the original worker still owns a live claim, its provider
            # request may be in flight.  Leave the item pending so that worker
            # can publish the receipt; after the claim expires, fail closed as
            # UNKNOWN instead of replaying a possibly accepted mutation.
            if (
                item.claim_token is not None
                and item.claimed_until is not None
                and item.claimed_until > _now()
            ):
                return False
            # The previous worker's claim is no longer live.  Preserve a
            # durable UNKNOWN attempt and receipt before exposing the terminal
            # item state; otherwise a recovery delivery would leave only an
            # item flag with no auditable request outcome.
            latest.outcome = "UNKNOWN"
            latest.retryable = False
            latest.response = {"reason": "attempt_claim_expired"}
            session.add(latest)
            session.add(
                ReceiptRow(
                    tenant_id=item.tenant_id,
                    attempt_id=latest.id,
                    outcome="UNKNOWN",
                    request_id=latest.request_id,
                    response={"reason": "attempt_claim_expired"},
                )
            )
            item.execution_result = "UNKNOWN"
            item.observation_state = "NEEDS_REVIEW"
            item.claim_token = None
            item.claimed_until = None
        return False
    if latest is not None and latest.retryable and latest.response.get("retry_at"):
        if datetime.fromisoformat(latest.response["retry_at"]) > _now():
            return False
    return True


def _recover_cancelled_claims(session: Session, task: ManagementTask) -> None:
    """调用方已锁父任务；取消后只归档过期领取，绝不进入平台写入路径。"""
    from sqlalchemy import or_

    from app.modules.ad_management.reconciliation import queue_reconciliation
    from app.modules.ad_management.recovery_dispatch import schedule_wakeup

    now = _now()
    rows = session.exec(
        select(ManagementTaskItem)
        .where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == task.tenant_id,
            ManagementTaskItem.execution_result == "PENDING",
            col(ManagementTaskItem.claim_token).is_not(None),
            or_(
                ManagementTaskItem.claimed_until <= now,
                col(ManagementTaskItem.claimed_until).is_(None),
            ),
        )
        .order_by(ManagementTaskItem.id)
        .with_for_update()
    ).all()
    unknown_ids = []
    for item in rows:
        # 复用请求围栏：记录过请求但未取得回执的领取过期后必须变UNKNOWN。
        # 明确未发送的领取才可取消；不能把缺少回执解释为平台拒绝。
        attempts = _attempts(session, item)
        _can_run(session, item, attempts)
        if (
            item.execution_result == "PENDING"
            and attempts
            and attempts[-1].outcome == "REJECTED"
        ):
            item.execution_result = "REJECTED"
        if item.execution_result in {"PENDING", "NOT_SENT"}:
            item.execution_result = "CANCELLED"
            item.delivery_status = "CANCELLED"
            item.reason = "cancelled"
        elif item.execution_result == "UNKNOWN":
            unknown_ids.append(item.id)
        item.claim_token = None
        item.claimed_until = None
        session.add(item)
    session.flush()
    if unknown_ids:
        queue_reconciliation(session, task, tuple(unknown_ids))
    # 提前到达的恢复消息不能消耗尚未到期领取的唯一唤醒机会。
    next_expiry = session.exec(
        select(func.min(ManagementTaskItem.claimed_until)).where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == task.tenant_id,
            ManagementTaskItem.execution_result == "PENDING",
            col(ManagementTaskItem.claim_token).is_not(None),
            ManagementTaskItem.claimed_until > now,
        )
    ).one()
    if next_expiry is not None:
        schedule_wakeup(
            session, task, next_expiry + timedelta(seconds=1), kind="claim", quantum=30
        )


def _same_frozen_value(
    session: Session, item: ManagementTaskItem, ref: EntityRef
) -> None:
    """Verify the local observation still equals the immutable preview value."""
    capability = item.capability or {}
    if item.original_value is None:
        raise DomainError("management_observation_conflict", "冻结目标原始值缺失")
    if (
        item.material_use is not None
        and capability.get("operation") == "set_material_status"
    ):
        use = _material(item.material_use)
        assert use is not None
        if use.ad_ref != ref:
            raise DomainError(
                "management_observation_conflict", "冻结素材与管理广告不一致"
            )
        row = locate_material_use(session, use)
        if row is None:
            raise DomainError(
                "management_observation_conflict", "冻结素材当前观测不存在"
            )
        if not any(
            current.use_ref == use for current in current_ad_materials(session, ref)
        ):
            # 预览后平台可能换发新 ID；旧选择不得继续操作历史记录，需刷新后重新选择。
            raise DomainError(
                "management_material_replaced", "素材投放记录已更新，请刷新后重新选择"
            )
        current = row.operation_status
        if current is None:
            raise DomainError("management_observation_conflict", "冻结素材当前值缺失")
    else:
        row = session.get(
            AdObject, (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id)
        )
        if row is None:
            # Some management dimensions have no directory row.  Capability
            # evidence cannot replace the current immutable observation.
            raise DomainError(
                "management_observation_conflict", "冻结目标当前观测不存在"
            )
        expected_parent = _ref(item.parent_ref) if item.parent_ref else None
        actual_parent = row.parent_ref
        if (expected_parent is None) != (actual_parent is None) or (
            expected_parent is not None
            and actual_parent is not None
            and (
                expected_parent.tenant_id != actual_parent.tenant_id
                or expected_parent.advertiser_id != actual_parent.advertiser_id
                or expected_parent.kind != actual_parent.kind
                or expected_parent.remote_id != actual_parent.remote_id
            )
        ):
            raise DomainError("management_parent_conflict", "管理目标父级关系已变化")
        expected_channel = capability.get("source_channel")
        expected_connection = capability.get("source_connection_id")
        if expected_channel and row.source_channel != expected_channel:
            raise DomainError("management_source_conflict", "目录对象来源通道已变化")
        if expected_connection and str(row.source_connection_id) != str(
            expected_connection
        ):
            raise DomainError("management_source_conflict", "目录对象来源连接已变化")
        operation = str(capability.get("operation"))
        if operation == "rename":
            # 改名的冻结原值是目录名称；名称已被他人修改则不覆盖。
            current = row.name.strip()
        elif operation == "set_status":
            current = row.operation_status
        else:
            key = "roas_bid" if operation == "update_roas" else "budget"
            current = (row.configuration or {}).get(key)
    if current is None:
        raise DomainError("management_observation_conflict", "冻结目标当前值缺失")
    if str(current) != str(item.original_value):
        raise DomainError("management_original_changed", "管理目标原始值已变化")


def _locked_item(session: Session, item_id: UUID, *, lock_task: bool = True):
    """取消先锁父任务，修改也采用同一锁序，防止并发回执与取消形成死锁。"""
    if lock_task:
        scope = session.exec(
            select(ManagementTaskItem.task_id, ManagementTaskItem.tenant_id).where(
                ManagementTaskItem.id == item_id
            )
        ).one_or_none()
        if scope is None:
            return None
        task = session.exec(
            select(ManagementTask)
            .where(ManagementTask.id == scope[0], ManagementTask.tenant_id == scope[1])
            .with_for_update()
        ).one_or_none()
        if task is None:
            return None
    return session.exec(
        select(ManagementTaskItem)
        .where(ManagementTaskItem.id == item_id)
        .with_for_update()
    ).one_or_none()


def _update_task(session: Session, task_id: UUID, tenant_id: UUID) -> None:
    task = session.exec(
        select(ManagementTask)
        .where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == tenant_id,
        )
        .with_for_update()
    ).one_or_none()
    if task is None:
        return
    if task.status == "PREPARING":
        return
    # 万条任务只读取状态聚合，不在每个回执后物化整批JSON和逐项查询attempt。
    rows = session.exec(
        select(
            ManagementTaskItem.execution_result,
            ManagementTaskItem.observation_state,
            func.count(),
        )
        .where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == tenant_id,
        )
        .group_by(
            ManagementTaskItem.execution_result, ManagementTaskItem.observation_state
        )
    ).all()
    totals: dict[str, int] = {}
    for result, _observation, total in rows:
        totals[result] = totals.get(result, 0) + int(total)
    counts = dict(task.counts or {})
    for key in (
        "accepted",
        "rejected",
        "unknown",
        "not_sent",
        "pending",
        "unsupported",
    ):
        counts[key] = totals.get(key.upper(), 0)
    counts["refresh_pending"] = sum(
        int(total)
        for result, observation, total in rows
        if result == "ACCEPTED" and observation == "REFRESH_PENDING"
    )
    task.counts = counts
    latest_retryable = (
        select(ManagementRequestAttempt.retryable)
        .where(
            ManagementRequestAttempt.task_item_id == ManagementTaskItem.id,
            ManagementRequestAttempt.tenant_id == tenant_id,
        )
        .order_by(col(ManagementRequestAttempt.attempt).desc())
        .limit(1)
        .correlate(ManagementTaskItem)
        .scalar_subquery()
    )
    retry_pending = (
        session.exec(
            select(ManagementTaskItem.id)
            .where(
                ManagementTaskItem.task_id == task.id,
                ManagementTaskItem.tenant_id == tenant_id,
                ManagementTaskItem.execution_result == "NOT_SENT",
                latest_retryable == True,  # noqa: E712
            )
            .limit(1)
        ).first()
        is not None
        if totals.get("NOT_SENT")
        else False
    )
    statuses = set(totals)
    if task.status == "CANCELLED":
        pass  # 取消后仍可记录在途回执，但不能复活任务或发送剩余项。
    elif "PENDING" in statuses or retry_pending:
        task.status = "RUNNING"
    elif "UNKNOWN" in statuses:
        task.status = "NEEDS_REVIEW"
    elif statuses and statuses <= {"ACCEPTED", "NO_CHANGE"}:
        task.status = "SUCCEEDED"
    elif statuses:
        task.status = "PARTIAL" if statuses & {"ACCEPTED", "NO_CHANGE"} else "FAILED"
    task.updated_at = _now()
    session.add(task)


def _payload(command: ManagementCommand) -> dict[str, Any]:
    return {
        "operation": command.operation,
        "entity_kind": command.entity_kind,
        "ref": {
            "advertiser_id": command.ref.advertiser_id,
            "kind": command.ref.kind,
            "remote_id": command.ref.remote_id,
        },
        "original": dict(command.original),
        "desired": dict(command.desired),
    }


def _evidence(receipt: ManagementReceipt) -> dict[str, Any]:
    evidence = receipt.evidence
    result: dict[str, Any] = (
        {}
        if evidence is None
        else {
            "request_id": evidence.request_id,
            "mcp_request_id": evidence.mcp_request_id,
            "remote_task_id": evidence.remote_task_id,
            "remote_code": evidence.remote_code,
        }
    )
    if receipt.reason:
        result["reason"] = receipt.reason
    if receipt.retry_after_ms is not None:
        result["retry_after_ms"] = receipt.retry_after_ms
    return result


def _persist_outcome(
    database_engine: Engine,
    *,
    item_id: UUID,
    token: UUID,
    generation: int,
    attempt_no: int,
    outcome: str,
    request_id: str | None,
    retryable: bool,
    response: dict[str, Any],
    lease: MutationLease,
    context: TenantContext,
    route: FrozenTikTokRoute,
    update_summary: bool = True,
    enqueue_retry: bool = True,
) -> bool:
    with Session(database_engine) as session, session.begin():
        item = _locked_item(session, item_id, lock_task=update_summary)
        if (
            item is None
            or item.claim_token != token
            or item.claim_generation != generation
        ):
            return False
        # Provider acceptance is only written by the current fenced owner.  A
        # stale owner leaves the durable attempt untouched for explicit review.
        # 明确未发送的回执只需数据库领取围栏；权限撤回或 Redis 失效本身
        # 就可能是拦截原因，不能因无法再次通过该门禁而改成 UNKNOWN。
        if outcome != "NOT_SENT":
            lease.fence()
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=_ref(item.ref).advertiser_id,
                capability="ads_manage",
                operation=(item.capability or {}).get("operation"),
                entity_kind=(item.capability or {}).get("entity_kind"),
            )
        attempt = session.exec(
            select(ManagementRequestAttempt)
            .where(
                ManagementRequestAttempt.tenant_id == item.tenant_id,
                ManagementRequestAttempt.task_item_id == item.id,
                ManagementRequestAttempt.attempt == attempt_no,
            )
            .with_for_update()
        ).one_or_none()
        if attempt is None:
            # Generation is the attempt identity for this worker; a missing row
            # is a fence failure rather than permission to create a duplicate.
            return False
        attempt.outcome = outcome
        attempt.request_id = request_id
        quota_wait = response.get("reason") in {
            "admission_deferred",
            "management_local_deferred",
        }
        transport_attempts = sum(
            row.response.get("reason")
            not in {"admission_deferred", "management_local_deferred"}
            for row in _attempts(session, item)
            if row.id != attempt.id
        ) + (0 if quota_wait else 1)
        # 本地额度等待没有平台发送，不能把排队三次误当成传输重试耗尽。
        retryable = (
            retryable
            and outcome == "NOT_SENT"
            and (quota_wait or transport_attempts < 3)
        )
        if retryable:
            delay = max(
                0.1 if quota_wait else 5 * 2 ** max(0, transport_attempts - 1),
                (response.get("retry_after_ms") or 0) / 1000,
            )
            retry_at = _now() + timedelta(seconds=delay)
            response = {**response, "retry_at": retry_at.isoformat()}
            if enqueue_retry:
                task = session.get(ManagementTask, item.task_id)
                assert task is not None
                from app.modules.ad_management.recovery_dispatch import schedule_wakeup

                schedule_wakeup(session, task, retry_at, kind="retry")
        elif outcome == "NOT_SENT" and not quota_wait and transport_attempts >= 3:
            response = {**response, "retry_exhausted": True}
        attempt.retryable = retryable
        attempt.response = response
        session.add(attempt)
        session.add(
            ReceiptRow(
                tenant_id=item.tenant_id,
                attempt_id=attempt.id,
                outcome=outcome,
                request_id=request_id,
                response=response,
            )
        )
        item.execution_result = (
            "CONFLICT"
            if outcome == "NOT_SENT"
            and response.get("reason") == "management_original_changed"
            else outcome
        )
        item.reason = response.get("reason")
        item.delivery_status = "DELIVERED" if outcome == "ACCEPTED" else outcome
        item.observation_state = (
            "ACCEPTED"
            if outcome == "ACCEPTED"
            else ("NEEDS_REVIEW" if outcome == "UNKNOWN" else None)
        )
        item.request_attribution = request_id
        item.claim_token = None
        item.claimed_until = None
        session.add(item)
        if outcome == "UNKNOWN":
            from app.modules.ad_management.reconciliation import queue_reconciliation

            task = session.get(ManagementTask, item.task_id)
            if task is not None:
                queue_reconciliation(session, task, (item.id,))
        if update_summary:
            _update_task(session, item.task_id, item.tenant_id)
    return True


def _refresh_after_commit(
    database_engine: Engine,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    ref: EntityRef,
) -> None:
    return _refresh_many_after_commit(
        database_engine, context=context, route=route, refs=(ref,)
    )


def _refresh_many_after_commit(
    database_engine: Engine,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    refs: tuple[EntityRef, ...],
) -> None:
    # 一个物理批次只登记一份定向回读，不能再按20成员重复锁BC和投递。
    del context
    if not settings.ADS_SYNC_ENABLED:
        return
    with Session(database_engine) as session, session.begin():
        plan = session.get(AdSyncBc, (route.tenant_id, route.bc_id))
        if plan is None or not plan.enabled:
            return
        request_directory(
            session,
            tenant_id=route.tenant_id,
            bc_id=route.bc_id,
            frozen_route=plan.frozen_route,
            actor_id=plan.actor_id,
            advertiser_ids=list(dict.fromkeys(ref.advertiser_id for ref in refs)),
            scope="targeted",
            refs=list(refs),
        )
        plan_hot(
            session,
            plan,
            active=list(dict.fromkeys(ref.advertiser_id for ref in refs)),
            now=datetime.now(UTC),
            tier="manual",
        )


def _set_refresh_state(
    database_engine: Engine,
    item_id: UUID,
    *,
    pending: bool,
    error: str | None = None,
) -> None:
    """Persist targeted-refresh progress independently of the write receipt."""
    with Session(database_engine) as session, session.begin():
        item = _locked_item(session, item_id)
        if item is None or item.execution_result != "ACCEPTED":
            return
        item.observation_state = "REFRESH_PENDING" if pending else "ACCEPTED"
        if pending:
            item.reason = error or "targeted_refresh_failed"
            task = session.get(ManagementTask, item.task_id)
            if task is not None:
                enqueue_after_commit(
                    session,
                    context=TenantContext(
                        tenant_id=task.tenant_id,
                        actor_id=task.actor_id,
                        role="operator",
                    ),
                    task_name="ad_management.execute",
                    task_key=f"ad_management.refresh:{item.id}:{uuid4()}",
                    payload={"task_id": str(task.id), "generation": 1},
                )
        elif item.reason == "targeted_refresh_failed":
            item.reason = None
        session.add(item)
        _update_task(session, item.task_id, item.tenant_id)


def _retry_targeted_refresh(database_engine: Engine, item_id: UUID) -> None:
    """Retry a durable post-write refresh without replaying the mutation."""
    with Session(database_engine) as session:
        item = _locked_item(session, item_id)
        if item is None or item.execution_result != "ACCEPTED":
            return
        task = session.get(ManagementTask, item.task_id)
        if task is None:
            return
        route = _route(task)
        context = TenantContext(
            tenant_id=task.tenant_id, actor_id=task.actor_id, role="operator"
        )
        ref = _ref(item.ref)
        session.commit()
    try:
        _refresh_after_commit(database_engine, context=context, route=route, ref=ref)
    except Exception:
        _set_refresh_state(database_engine, item_id, pending=True)
    else:
        _set_refresh_state(database_engine, item_id, pending=False)


def _mark_unknown_after_persist_failure(
    database_engine: Engine,
    *,
    item_id: UUID,
    token: UUID,
    generation: int,
    attempt_no: int,
) -> None:
    """Turn this owner's committed pre-attempt into UNKNOWN on receipt failure."""
    try:
        with Session(database_engine) as session, session.begin():
            item = _locked_item(session, item_id)
            if (
                item is None
                or item.claim_token != token
                or item.claim_generation != generation
            ):
                return
            attempt = session.exec(
                select(ManagementRequestAttempt)
                .where(
                    ManagementRequestAttempt.tenant_id == item.tenant_id,
                    ManagementRequestAttempt.task_item_id == item.id,
                    ManagementRequestAttempt.attempt == attempt_no,
                )
                .with_for_update()
            ).one_or_none()
            if attempt is None or attempt.outcome != "NOT_SENT":
                return
            attempt.outcome = "UNKNOWN"
            attempt.retryable = False
            attempt.response = {"reason": "receipt_commit_failed"}
            session.add(attempt)
            session.add(
                ReceiptRow(
                    tenant_id=item.tenant_id,
                    attempt_id=attempt.id,
                    outcome="UNKNOWN",
                    response={"reason": "receipt_commit_failed"},
                )
            )
            item.execution_result = "UNKNOWN"
            item.observation_state = "NEEDS_REVIEW"
            item.claim_token = None
            item.claimed_until = None
            session.add(item)
            _update_task(session, item.task_id, item.tenant_id)
    except Exception:
        # A second database failure leaves the durable pre-attempt in place;
        # its ATTEMPT_RECORDED marker still blocks an automatic replay.
        return


def _mark_unknown_after_fence_failure(
    database_engine: Engine,
    *,
    item_id: UUID,
    token: UUID,
    generation: int,
    attempt_no: int,
) -> None:
    """Fail closed after a sent request cannot pass its receipt fence.

    The token and generation predicates ensure an old worker cannot overwrite
    a newer claim that has already taken ownership of the item.
    """
    try:
        with Session(database_engine) as session, session.begin():
            item = _locked_item(session, item_id)
            if (
                item is None
                or item.claim_token != token
                or item.claim_generation != generation
            ):
                return
            attempt = session.exec(
                select(ManagementRequestAttempt)
                .where(
                    ManagementRequestAttempt.tenant_id == item.tenant_id,
                    ManagementRequestAttempt.task_item_id == item.id,
                    ManagementRequestAttempt.attempt == attempt_no,
                )
                .with_for_update()
            ).one_or_none()
            if attempt is None or attempt.outcome != "NOT_SENT":
                return
            attempt.outcome = "UNKNOWN"
            attempt.retryable = False
            attempt.response = {"reason": "receipt_fence_failed"}
            session.add(attempt)
            session.add(
                ReceiptRow(
                    tenant_id=item.tenant_id,
                    attempt_id=attempt.id,
                    outcome="UNKNOWN",
                    response={"reason": "receipt_fence_failed"},
                )
            )
            item.execution_result = "UNKNOWN"
            item.observation_state = "NEEDS_REVIEW"
            item.claim_token = None
            item.claimed_until = None
            session.add(item)
            _update_task(session, item.task_id, item.tenant_id)
    except Exception:
        # Preserve the committed pre-attempt when this recovery transaction is
        # itself unavailable; the next delivery will fail closed after expiry.
        return


def execute_item(database_engine: Engine, item_id: UUID) -> bool | None:
    """Execute one item; failures are persisted per item and never abort siblings."""
    redis_client = _redis()
    lease: MutationLease | None = None
    owner = uuid4()
    try:
        with Session(database_engine) as session:
            item = _locked_item(session, item_id)
            if item is None:
                raise DomainError("management_item_not_found", "管理任务项不存在")
            task = session.exec(
                select(ManagementTask).where(
                    ManagementTask.id == item.task_id,
                    ManagementTask.tenant_id == item.tenant_id,
                )
            ).one_or_none()
            if task is None:
                raise DomainError("management_task_not_found", "管理任务不存在")
            if (
                item.execution_result == "ACCEPTED"
                and item.observation_state == "REFRESH_PENDING"
            ):
                session.commit()
                _retry_targeted_refresh(database_engine, item_id)
                return
            if task.status == "CANCELLED":
                _recover_cancelled_claims(session, task)
                _update_task(session, task.id, task.tenant_id)
                session.commit()
                return
            attempts = _attempts(session, item)
            if not _can_run(session, item, attempts):
                _update_task(session, task.id, task.tenant_id)
                session.commit()
                return
            route = _route(task)
            context = TenantContext(
                tenant_id=task.tenant_id, actor_id=task.actor_id, role="operator"
            )
            ref = _ref(item.ref)
            command = _command(item)
            if (
                route.tenant_id != item.tenant_id
                or ref.tenant_id != item.tenant_id
                or route.channel not in {"OFFICIAL_API", "OFFICIAL_MCP"}
            ):
                raise DomainError(
                    "frozen_route_scope_mismatch", "冻结路由与管理目标不一致"
                )
            if (item.capability or {}).get("route") != route.model_dump(mode="json"):
                raise DomainError("frozen_route_changed", "任务项冻结路由已变化")
            # claim_mutation shares the lock domain with build.disable_adgroup;
            # Redis failure is a per-item failure, never a whole-task abort.
            lease = claim_mutation(
                session,
                (ref,),
                owner,
                redis_client=redis_client,
                lease_seconds=_CLAIM_SECONDS,
            )
            generation = item.claim_generation + (1 if attempts else 0)
            item.claim_generation = generation
            item.claim_token = owner
            item.claimed_until = _now() + timedelta(seconds=_CLAIM_SECONDS)
            task.status = "RUNNING"
            # Claim and pre-attempt are one short transaction.  A duplicate
            # delivery cannot observe an unclaimed item between these writes and
            # open a second provider request.
            attempt_no = max([row.attempt for row in attempts], default=0) + 1
            session.add(
                ManagementRequestAttempt(
                    tenant_id=item.tenant_id,
                    task_item_id=item.id,
                    attempt=attempt_no,
                    outcome="NOT_SENT",
                    payload=_payload(command),
                    response={},
                    retryable=False,
                )
            )
            item.execution_result = "PENDING"
            item.delivery_status = "ATTEMPT_RECORDED"
            session.add_all([item, task])
            session.flush()
            from app.modules.ad_management.recovery_dispatch import schedule_wakeup

            schedule_wakeup(
                session,
                task,
                item.claimed_until + timedelta(seconds=1),
                kind="claim",
                quantum=30,
            )
            session.commit()
            # Attempt row identity is separate from Redis claim generation.

        assert lease is not None
        try:
            lease.fence(redis_client)
            with Session(database_engine) as check:
                current = check.exec(
                    select(ManagementTaskItem).where(ManagementTaskItem.id == item_id)
                ).one()
                task = check.get(ManagementTask, current.task_id)
                assert task is not None
                route = _route(task)
                context = TenantContext(
                    tenant_id=task.tenant_id, actor_id=task.actor_id, role="operator"
                )
                ref = _ref(current.ref)
                verify_route(
                    check,
                    context=context,
                    route=route,
                    advertiser_id=ref.advertiser_id,
                    capability="ads_manage",
                    operation=(current.capability or {}).get("operation"),
                    entity_kind=(current.capability or {}).get("entity_kind"),
                )
                _same_frozen_value(check, current, ref)
                command = _command(current)
        except DomainError:
            raise
        except Exception as error:
            # 此处尚未打开平台网关，确定没有发送，不能遗留执行中或误记 UNKNOWN。
            raise DomainError(
                "management_preflight_failed", "发送前素材或对象校验失败"
            ) from error

        def before_provider_request() -> None:
            """Re-fence and re-read the frozen object at each adapter send gate."""
            lease.fence(redis_client)
            with Session(database_engine) as preflight:
                latest = preflight.exec(
                    select(ManagementTaskItem).where(ManagementTaskItem.id == item_id)
                ).one_or_none()
                if latest is None:
                    raise DomainError("management_item_not_found", "管理任务项不存在")
                latest_task = preflight.get(ManagementTask, latest.task_id)
                if latest_task is None:
                    raise DomainError("management_task_not_found", "管理任务不存在")
                if latest_task.status == "CANCELLED":
                    raise DomainError("management_task_cancelled", "管理任务已取消")
                latest_route = _route(latest_task)
                if latest_route != route or (latest.capability or {}).get(
                    "route"
                ) != route.model_dump(mode="json"):
                    raise DomainError("frozen_route_changed", "任务项冻结路由已变化")
                if latest.claim_token != owner or latest.claim_generation != generation:
                    raise DomainError("management_claim_stale", "管理任务领取已过期")
                latest_ref = _ref(latest.ref)
                verify_route(
                    preflight,
                    context=context,
                    route=route,
                    advertiser_id=latest_ref.advertiser_id,
                    capability="ads_manage",
                    operation=(latest.capability or {}).get("operation"),
                    entity_kind=(latest.capability or {}).get("entity_kind"),
                )
                _same_frozen_value(preflight, latest, latest_ref)

        provider_called = False
        try:
            deadline = _now() + timedelta(seconds=45)
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=deadline,
                before_request=before_provider_request,
            ) as gateway:
                lease.fence(redis_client)
                provider_called = True
                receipt = gateway.management.apply(command)
        except RemoteCallError as error:
            outcome = "UNKNOWN" if error.effect == "UNKNOWN" else "NOT_SENT"
            receipt = ManagementReceipt(
                outcome,
                error.evidence.request_id or error.evidence.mcp_request_id,
                outcome == "NOT_SENT"
                and error.code in (TRANSIENT_NOT_SENT | {"admission_deferred"}),
                error.evidence,
                reason=error.code,
                retry_after_ms=error.retry_after_ms,
            )
        except DomainError as error:
            outcome = "UNKNOWN" if provider_called else "NOT_SENT"
            receipt = ManagementReceipt(
                outcome,
                None,
                outcome == "NOT_SENT" and error.code in TRANSIENT_NOT_SENT,
                reason=error.code,
            )
        except Exception:
            outcome = "UNKNOWN" if provider_called else "NOT_SENT"
            receipt = ManagementReceipt(
                outcome,
                None,
                False,
                reason="management_result_unknown"
                if provider_called
                else "management_preflight_failed",
            )

        # The attempt number is looked up from SQL after the provider call.  It is
        # never inferred from Redis generation, preserving retries after restart.
        with Session(database_engine) as read:
            attempt = read.exec(
                select(ManagementRequestAttempt)
                .where(ManagementRequestAttempt.task_item_id == item_id)
                .order_by(col(ManagementRequestAttempt.attempt).desc())
            ).first()
            assert attempt is not None
            attempt_no = attempt.attempt
        try:
            persisted = _persist_outcome(
                database_engine,
                item_id=item_id,
                token=owner,
                generation=generation,
                attempt_no=attempt_no,
                outcome=receipt.outcome,
                request_id=receipt.request_id,
                retryable=receipt.retryable,
                response=_evidence(receipt),
                lease=lease,
                context=context,
                route=route,
            )
        except DomainError:
            # A stale lease or route/permission generation is itself a writeback
            # fence.  When the provider was reached, fail closed with a durable
            # UNKNOWN guarded by this worker's claim token and generation.
            if provider_called:
                _mark_unknown_after_fence_failure(
                    database_engine,
                    item_id=item_id,
                    token=owner,
                    generation=generation,
                    attempt_no=attempt_no,
                )
            persisted = False
        except Exception:
            _mark_unknown_after_persist_failure(
                database_engine,
                item_id=item_id,
                token=owner,
                generation=generation,
                attempt_no=attempt_no,
            )
            persisted = False
        if persisted and receipt.outcome == "ACCEPTED":
            try:
                _refresh_after_commit(
                    database_engine, context=context, route=route, ref=ref
                )
            except Exception:
                # Keep the accepted receipt while recording a durable refresh
                # retry state; a later management delivery only refreshes.
                _set_refresh_state(database_engine, item_id, pending=True)
            else:
                _set_refresh_state(database_engine, item_id, pending=False)
    except DomainError as error:
        if error.code == "mutation_busy":
            # 共享祖先锁竞争不是对象失败；没有发送也没有attempt，让编排层重排。
            return False
        # Claim/preflight failures are isolated to this item and are recorded as
        # a non-sent, non-retryable attempt where possible.
        with Session(database_engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            if item is not None and item.execution_result == "PENDING":
                item.reason = error.code
                # 目标原值已被他人改动（如名称被改）时记为冲突，不覆盖别人的修改。
                item.execution_result = (
                    "CONFLICT"
                    if error.code == "management_original_changed"
                    else "NOT_SENT"
                )
                # 已提交的预请求也要关闭，重复投递不能把本地失败误判为发送结果未知。
                attempts = _attempts(session, item)
                if item.claim_token == owner and attempts:
                    attempt = attempts[-1]
                    if attempt.outcome == "NOT_SENT":
                        attempt.retryable = False
                        attempt.response = {"reason": error.code}
                        session.add(attempt)
                        session.add(
                            ReceiptRow(
                                tenant_id=item.tenant_id,
                                attempt_id=attempt.id,
                                outcome="NOT_SENT",
                                response={"reason": error.code},
                            )
                        )
                item.claim_token = None
                item.claimed_until = None
                item.delivery_status = error.code
                item.observation_state = (
                    "NEEDS_REVIEW" if error.code.startswith("route_") else None
                )
                session.add(item)
                _update_task(session, item.task_id, item.tenant_id)
                session.commit()
    finally:
        if lease is not None:
            lease.release(redis_client)
        redis_client.close()


__all__ = ["execute_item"]
