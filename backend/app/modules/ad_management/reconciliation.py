"""受理与未知动作的只读核查：状态观察独立于写回执，永不据此重发写请求。"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import (
    DirectoryQuery,
    EntityRef,
    MaterialUseRef,
)
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.outbox import enqueue_many_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.models import (
    ManagementReceipt,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.schemas import ManagementItemPublic
from app.modules.tenants.permissions import require_tenant

OBSERVABLE_RESULTS = {"UNKNOWN", "ACCEPTED"}
FINAL_OBSERVATIONS = {"OBSERVED_MATCH", "OBSERVED_DIFFERENT", "RECONCILE_FAILED"}
MAX_RECONCILIATION_ATTEMPTS = 3


def _latest_attempt(session, item):
    return session.exec(
        select(ManagementRequestAttempt)
        .where(
            col(ManagementRequestAttempt.tenant_id) == item.tenant_id,
            col(ManagementRequestAttempt.task_item_id) == item.id,
        )
        .order_by(col(ManagementRequestAttempt.attempt).desc())
        .limit(1)
    ).first()


def _latest_request(session, attempt):
    if attempt is None:
        return None
    return session.exec(
        select(ManagementReceipt)
        .where(
            col(ManagementReceipt.tenant_id) == attempt.tenant_id,
            col(ManagementReceipt.attempt_id) == attempt.id,
            col(ManagementReceipt.response)["kind"].astext
            == "state_observation_request",
        )
        .order_by(
            col(ManagementReceipt.observed_at).desc(), col(ManagementReceipt.id).desc()
        )
        .limit(1)
    ).first()


def _validate_ids(item_ids, attempt):
    if (
        type(item_ids) is not tuple
        or not item_ids
        or any(not isinstance(i, UUID) for i in item_ids)
        or len(set(item_ids)) != len(item_ids)
        or type(attempt) is not int
        or not 1 <= attempt <= 3
    ):
        raise DomainError("dispatch_payload_invalid", "核查对象或次数无效")


def queue_reconciliation(
    session: Session,
    task: ManagementTask,
    item_ids: tuple[UUID, ...],
    attempt: int = 1,
    *,
    round_key: str = "auto",
    available_at: datetime | None = None,
):
    """同事务排队，每个消息最多20项；新人工round不会复用已发布的自动核查键。"""
    from app.modules.ad_management.reconciliation_tasks import RECONCILIATION_TASK

    _validate_ids(item_ids, attempt)
    if round_key != "auto":
        UUID(round_key)
    stored = session.exec(
        select(ManagementTask).where(
            col(ManagementTask.id) == task.id,
            col(ManagementTask.tenant_id) == task.tenant_id,
            col(ManagementTask.actor_id) == task.actor_id,
        )
    ).one_or_none()
    rows = session.exec(
        select(ManagementTaskItem)
        .where(
            col(ManagementTaskItem.tenant_id) == task.tenant_id,
            col(ManagementTaskItem.task_id) == task.id,
            col(ManagementTaskItem.id).in_(item_ids),
        )
        .order_by(col(ManagementTaskItem.id))
        .with_for_update()
    ).all()
    if stored is None or len(rows) != len(item_ids):
        raise DomainError("action_forbidden", "核查对象范围不匹配")
    # 自动补排可能一次覆盖整笔大任务，原请求与当前round也按集合读取。
    sources = {
        value.task_item_id: value
        for value in session.exec(
            select(ManagementRequestAttempt)
            .where(
                col(ManagementRequestAttempt.tenant_id) == task.tenant_id,
                col(ManagementRequestAttempt.task_item_id).in_(item_ids),
            )
            .distinct(col(ManagementRequestAttempt.task_item_id))
            .order_by(
                col(ManagementRequestAttempt.task_item_id),
                col(ManagementRequestAttempt.attempt).desc(),
            )
        ).all()
    }
    requests = (
        {
            value.attempt_id: value
            for value in session.exec(
                select(ManagementReceipt)
                .where(
                    col(ManagementReceipt.tenant_id) == task.tenant_id,
                    col(ManagementReceipt.attempt_id).in_(
                        [value.id for value in sources.values()]
                    ),
                    col(ManagementReceipt.response)["kind"].astext
                    == "state_observation_request",
                )
                .distinct(col(ManagementReceipt.attempt_id))
                .order_by(
                    col(ManagementReceipt.attempt_id),
                    col(ManagementReceipt.observed_at).desc(),
                    col(ManagementReceipt.id).desc(),
                )
            ).all()
        }
        if sources
        else {}
    )
    ids = []
    for item in rows:
        if item.execution_result not in OBSERVABLE_RESULTS or (
            round_key == "auto" and item.observation_state in FINAL_OBSERVATIONS
        ):
            continue
        mutation_attempt = sources.get(item.id)
        requested = requests.get(mutation_attempt.id) if mutation_attempt else None
        if (
            round_key == "auto"
            and requested
            and requested.response.get("round") != "auto"
        ):
            continue  # 人工核查已取代旧自动round。
        if mutation_attempt and (
            requested is None
            or (requested.response.get("round"), requested.response.get("attempt"))
            != (round_key, attempt)
        ):
            session.add(
                ManagementReceipt(
                    tenant_id=task.tenant_id,
                    attempt_id=mutation_attempt.id,
                    outcome="UNKNOWN",
                    observed_at=max(
                        datetime.now(UTC),
                        requested.observed_at + timedelta(microseconds=1),
                    )
                    if requested
                    else datetime.now(UTC),
                    response={
                        "kind": "state_observation_request",
                        "round": round_key,
                        "attempt": attempt,
                    },
                )
            )
        item.observation_state = "RECONCILE_REQUESTED"
        session.add(item)
        ids.append(item.id)
    entries = []
    for begin in range(0, len(ids), 20):
        members = tuple(ids[begin : begin + 20])
        digest = sha256(",".join(map(str, members)).encode()).hexdigest()[:24]
        entries.append(
            (
                RECONCILIATION_TASK,
                f"ad-management-reconcile:{task.id}:{round_key}:{attempt}:{digest}",
                {
                    "task_id": str(task.id),
                    "item_ids": list(map(str, members)),
                    "attempt": attempt,
                    "round": round_key,
                },
                available_at,
            )
        )
    if entries:
        enqueue_many_after_commit(
            session,
            context=TenantContext(
                tenant_id=task.tenant_id, actor_id=task.actor_id, role="operator"
            ),
            entries=entries,
        )
    session.flush()


def reconcile_item(
    session: Session, context: TenantContext, item_id: UUID
) -> ManagementItemPublic:
    from app.modules.ad_management.execution import _public

    item = session.exec(
        select(ManagementTaskItem).where(
            col(ManagementTaskItem.id) == item_id,
            col(ManagementTaskItem.tenant_id) == context.tenant_id,
        )
    ).one_or_none()
    if item is None:
        raise DomainError("management_item_not_found", "管理任务项不存在")
    task = session.exec(
        select(ManagementTask).where(
            col(ManagementTask.id) == item.task_id,
            col(ManagementTask.tenant_id) == context.tenant_id,
            col(ManagementTask.actor_id) == context.actor_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("action_forbidden", "管理任务操作者不匹配")
    if item.execution_result in OBSERVABLE_RESULTS:
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="read",
        )
        queue_reconciliation(session, task, (item.id,), round_key=str(uuid4()))
    return _public(item)


def _current_request(session, item, round_key, attempt):
    source = _latest_attempt(session, item)
    requested = _latest_request(session, source)
    return source, bool(
        requested
        and requested.response.get("round") == round_key
        and requested.response.get("attempt") == attempt
    )


def _read_authorized(engine, tenant_id, actor_id, route, advertiser_id):
    with Session(engine) as session:
        context = require_tenant(
            session, actor_id=actor_id, tenant_id=tenant_id, action="read"
        )
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=advertiser_id,
            capability="read",
        )
        return context


def _value(entity, item, page):
    operation = item.capability.get("operation")
    if item.material_use:
        raw = item.material_use
        use = MaterialUseRef(
            EntityRef(
                **cast(
                    dict[str, Any],
                    {**raw["ad_ref"], "tenant_id": UUID(raw["ad_ref"]["tenant_id"])},
                )
            ),
            raw["platform_material_id"],
            raw.get("ad_material_id"),
            raw["material_type"],
        )
        found = [value for value in page.materials if value.use_ref == use]
        if not page.materials_complete or len(found) != 1 or not found[0].complete:
            raise ValueError("material evidence incomplete")
        value = found[0].operation_status
    elif operation == "rename":
        value = entity.name
    elif operation == "set_status":
        value = entity.operation_status
    elif operation in {"update_budget", "update_roas"}:
        value = entity.configuration.get(
            "budget" if operation == "update_budget" else "roas_bid"
        )
    else:
        raise ValueError("unsupported observation")
    if value is None or item.final_value is None:
        raise ValueError("missing observation value")
    if operation in {"update_budget", "update_roas"}:
        actual, desired = Decimal(str(value)), Decimal(item.final_value)
        if not actual.is_finite() or not desired.is_finite():
            raise ValueError("invalid numeric observation")
        matches = actual == desired
    else:
        matches = str(value) == str(item.final_value)
    return str(value), matches


def _persist_observation(
    engine,
    tenant_id,
    actor_id,
    task_id,
    item_id,
    attempt,
    round_key,
    *,
    observation=None,
    retry_after_ms=0,
    terminal=False,
):
    with Session(engine) as session:
        task = session.exec(
            select(ManagementTask).where(
                col(ManagementTask.id) == task_id,
                col(ManagementTask.tenant_id) == tenant_id,
                col(ManagementTask.actor_id) == actor_id,
            )
        ).one_or_none()
        item = session.exec(
            select(ManagementTaskItem)
            .where(
                col(ManagementTaskItem.id) == item_id,
                col(ManagementTaskItem.task_id) == task_id,
                col(ManagementTaskItem.tenant_id) == tenant_id,
            )
            .with_for_update()
        ).one_or_none()
        if (
            task is None
            or item is None
            or item.execution_result not in OBSERVABLE_RESULTS
        ):
            return
        source, current = _current_request(session, item, round_key, attempt)
        if source is None:
            item.observation_state = "RECONCILE_FAILED"
            session.add(item)
            session.commit()
            return
        if not current or item.observation_state in {
            "OBSERVED_MATCH",
            "OBSERVED_DIFFERENT",
        }:
            return
        duplicate = session.exec(
            select(col(ManagementReceipt.id)).where(
                col(ManagementReceipt.attempt_id) == source.id,
                col(ManagementReceipt.response)["kind"].astext == "state_observation",
                col(ManagementReceipt.response)["round"].astext == round_key,
                col(ManagementReceipt.response)["attempt"].as_integer() == attempt,
            )
        ).first()
        if duplicate:
            return
        if observation is not None:
            # 网络请求后再核验权限；撤权后不能发布刚取回的账户数据。
            try:
                context = require_tenant(
                    session, actor_id=actor_id, tenant_id=tenant_id, action="read"
                )
                verify_route(
                    session,
                    context=context,
                    route=FrozenTikTokRoute.model_validate(task.route),
                    advertiser_id=item.ref["advertiser_id"],
                    capability="read",
                )
            except DomainError:
                observation, terminal = None, True
        state = (
            ("OBSERVED_MATCH" if observation["matches"] else "OBSERVED_DIFFERENT")
            if observation
            else (
                "RECONCILE_FAILED"
                if terminal or attempt >= MAX_RECONCILIATION_ATTEMPTS
                else "RECONCILE_REQUESTED"
            )
        )
        item.observation_state = state
        session.add(item)
        session.add(
            ManagementReceipt(
                tenant_id=tenant_id,
                attempt_id=source.id,
                outcome="UNKNOWN",
                response={
                    "kind": "state_observation",
                    "round": round_key,
                    "attempt": attempt,
                    "state": state,
                    **(observation or {"reason": "read_evidence_unavailable"}),
                },
            )
        )
        if observation is None and state != "RECONCILE_FAILED":
            queue_reconciliation(
                session,
                task,
                (item.id,),
                attempt + 1,
                round_key=round_key,
                available_at=datetime.now(UTC)
                + timedelta(
                    milliseconds=max(15000 * 2 ** (attempt - 1), retry_after_ms)
                ),
            )
        session.commit()


def reconcile_unknown_items(
    engine,
    tenant_id: UUID,
    actor_id: UUID,
    task_id: UUID,
    item_ids: tuple[UUID, ...],
    attempt: int,
    *,
    round_key: str = "auto",
):
    """真实原路由定向读取，最多三轮；核查不改变 UNKNOWN/ACCEPTED 写入结果。"""
    _validate_ids(item_ids, attempt)
    if len(item_ids) > 20:
        raise DomainError("dispatch_payload_invalid", "单次核查最多20项")
    with Session(engine, expire_on_commit=False) as session:
        task = session.exec(
            select(ManagementTask).where(
                col(ManagementTask.id) == task_id,
                col(ManagementTask.tenant_id) == tenant_id,
                col(ManagementTask.actor_id) == actor_id,
            )
        ).one_or_none()
        rows = session.exec(
            select(ManagementTaskItem).where(
                col(ManagementTaskItem.task_id) == task_id,
                col(ManagementTaskItem.tenant_id) == tenant_id,
                col(ManagementTaskItem.id).in_(item_ids),
            )
        ).all()
        if task is None or len(rows) != len(item_ids):
            raise DomainError("action_forbidden", "核查对象范围不匹配")
        route = FrozenTikTokRoute.model_validate(task.route)
        if route.tenant_id != tenant_id or route.bc_id != task.bc_id:
            raise DomainError("action_forbidden", "核查路由范围不匹配")
        groups = defaultdict(list)
        for item in rows:
            if (
                item.execution_result not in OBSERVABLE_RESULTS
                or item.observation_state in FINAL_OBSERVATIONS
            ):
                continue
            source, current = _current_request(session, item, round_key, attempt)
            if source and current:
                groups[
                    (
                        item.ref["advertiser_id"],
                        item.ref["kind"],
                        item.capability.get("ad_type", "REGULAR"),
                    )
                ].append((item, source))
            elif source is None:
                item.observation_state = "RECONCILE_FAILED"
                session.add(item)
        session.commit()
        # 形成独立值副本，网络期间不隐式重开数据库查询。
        work = [
            (
                key,
                [
                    (item.model_copy(deep=True), source.model_copy(deep=True))
                    for item, source in values
                ],
            )
            for key, values in groups.items()
        ]
    for (advertiser_id, kind, ad_type), values in work:
        try:
            context = _read_authorized(
                engine, tenant_id, actor_id, route, advertiser_id
            )
        except DomainError:
            for item, _ in values:
                _persist_observation(
                    engine,
                    tenant_id,
                    actor_id,
                    task_id,
                    item.id,
                    attempt,
                    round_key,
                    terminal=True,
                )
            continue
        started = datetime.now(UTC)
        try:
            with (
                Redis.from_url(settings.REDIS_URL) as redis_client,
                open_tiktok_gateway(
                    database_engine=engine,
                    redis_client=redis_client,
                    context=context,
                    route=route,
                    task_deadline=started + timedelta(seconds=60),
                    before_request=lambda advertiser_id=advertiser_id: _read_authorized(
                        engine, tenant_id, actor_id, route, advertiser_id
                    ),
                ) as gateway,
            ):
                page = gateway.ads.read_page(
                    DirectoryQuery(
                        advertiser_id=advertiser_id,
                        kind=kind,
                        ad_type=ad_type,
                        page=1,
                        page_size=100,
                        ids=tuple(
                            dict.fromkeys(item.ref["remote_id"] for item, _ in values)
                        ),
                        parent_ids=(),
                        include_deleted=True,
                    )
                )
            if (
                not page.complete
                or page.next_page is not None
                or page.evidence.remote_code not in {None, 0}
            ):
                raise ValueError("incomplete directory response")
        except OperationalError, InterfaceError:
            raise
        except (
            DomainError,
            ValueError,
            KeyError,
            TypeError,
            TimeoutError,
            OSError,
        ) as error:
            delay = getattr(error, "retry_after_ms", None)
            for item, _ in values:
                _persist_observation(
                    engine,
                    tenant_id,
                    actor_id,
                    task_id,
                    item.id,
                    attempt,
                    round_key,
                    retry_after_ms=delay if isinstance(delay, int) and delay > 0 else 0,
                )
            continue
        for item, source in values:
            observation = None
            try:
                ref = EntityRef(
                    **cast(
                        dict[str, Any],
                        {**item.ref, "tenant_id": UUID(item.ref["tenant_id"])},
                    )
                )
                entities = [entity for entity in page.items if entity.ref == ref]
                if len(entities) != 1 or entities[0].observed_at < max(
                    started, source.request_at
                ):
                    raise ValueError("missing or stale remote evidence")
                value, matches = _value(entities[0], item, page)
                observation = {
                    "read_request_id": page.evidence.request_id
                    or page.evidence.mcp_request_id,
                    "value": value,
                    "matches": matches,
                    "remote_observed_at": entities[0].observed_at.isoformat(),
                }
            except ValueError, InvalidOperation:
                pass
            _persist_observation(
                engine,
                tenant_id,
                actor_id,
                task_id,
                item.id,
                attempt,
                round_key,
                observation=observation,
            )


__all__ = ["queue_reconciliation", "reconcile_unknown_items", "reconcile_item"]
