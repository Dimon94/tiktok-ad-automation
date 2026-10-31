"""Transactional outbox with at-least-once delivery and bounded tenant rounds.

Celery task_id is an identity, not automatic deduplication. A consumer must use
its own database business key to make repeated execution safe. Large workflows
must enqueue a bounded executable window instead of their entire future graph.
"""

import json
import logging
import math
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, literal, or_
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.jobs.tasks import dispatch_queue

logger = logging.getLogger(__name__)

# One expansion slot keeps a generated successor ahead of its own unit fanout.
# Other expansion messages never consume ordinary FIFO slots in this round.
_EXPANSION_TASK = "builds.expand_submission"
_COMPACTABLE_TASKS = frozenset({"builds.execute_step", "builds.execute_unit"})

_SECRET_NAMES = {
    "token",
    "cookie",
    "password",
    "secret",
    "authorization",
    "credential",
    "filebody",
    "filecontent",
    "filedata",
    "filebytes",
    "filebase64",
    "videobody",
    "videocontent",
    "videodata",
    "videobytes",
    "body",
    "content",
}


def validate_dispatch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept small JSON references/scalars, never credentials or file bodies."""

    def invalid() -> NoReturn:
        raise DomainError(
            "dispatch_payload_invalid", "任务参数只能包含内部标识和必要标量"
        )

    def check(value: object, depth: int = 0) -> None:
        if depth > 8:
            invalid()
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 128:
                    invalid()
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(word in normalized for word in _SECRET_NAMES):
                    invalid()
                check(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                check(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > 1024:
                invalid()
        elif value is None or isinstance(value, (bool, int)):
            pass
        elif isinstance(value, float) and math.isfinite(value):
            pass
        else:
            invalid()

    if not isinstance(payload, dict):
        invalid()
    check(payload)
    try:
        encoded = json.dumps(payload, allow_nan=False)
    except TypeError, ValueError, OverflowError:
        invalid()
    if len(encoded.encode()) > 16384:
        invalid()
    # Snapshot caller-owned objects: later mutation cannot change the dispatch.
    return cast(dict[str, Any], json.loads(encoded))


def _payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(encoded).hexdigest()


def enqueue_after_commit(
    session: Session,
    *,
    context: TenantContext,
    task_name: str,
    task_key: str,
    payload: dict[str, Any],
    _sync_contract: bool = False,
) -> UUID:
    """Write in the caller's transaction. Never commit or publish here."""
    return enqueue_many_after_commit(
        session,
        context=context,
        entries=[(task_name, task_key, payload, None)],
        _sync_contract=_sync_contract,
    )[0]


def enqueue_many_after_commit(
    session: Session,
    *,
    context: TenantContext,
    entries: list[tuple[str, str, dict[str, Any], datetime | None]],
    _sync_contract: bool = False,
) -> list[UUID]:
    """同一事务内批量写入 (task_name, task_key, payload, available_at)，按输入顺序返回 ID。

    批量共享领取一次要登记 200 条消息；逐条写入在生产 800 多万行的 outbox 上
    要 600 次往返、约 5 秒。这里固定为游标一次、插入一次、冲突核对一次。
    同一 task_key 已存在时语义与单条相同：配置一致则复用，不一致则拒绝。
    """
    if not entries:
        return []
    rows: list[dict[str, Any]] = []
    for task_name, task_key, payload, available_at in entries:
        dispatch_queue(task_name)
        payload = validate_dispatch_payload(payload)
        if not isinstance(task_key, str) or not task_key.strip() or len(task_key) > 255:
            raise DomainError("dispatch_payload_invalid", "任务标识无效")
        if not _sync_contract:
            from app.modules.sync_control.dispatch_envelope import (
                legacy_enqueue_allowed,
            )

            if not legacy_enqueue_allowed(
                session,
                context=context,
                task_name=task_name,
                task_key=task_key,
                payload=payload,
            ):
                raise DomainError(
                    "dispatch_payload_invalid", "V2同步必须由canonical领取签发消息"
                )
        rows.append(
            {
                "id": uuid4(),
                "tenant_id": context.tenant_id,
                "actor_id": context.actor_id,
                "task_name": task_name,
                "task_key": task_key,
                "payload": payload,
                "payload_digest": _payload_digest(payload),
                "available_at": available_at or datetime.now(UTC),
            }
        )
    keys = [task_key for _, task_key, _, _ in entries]
    if len(set(keys)) != len(keys):
        raise DomainError("dispatch_key_conflict", "同一批次内任务标识重复")
    session.exec(
        insert(DispatchTenantCursor)
        .values(tenant_id=context.tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    inserted: dict[str, UUID] = {
        task_key: identity
        for identity, task_key in session.exec(
            insert(PendingDispatch)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["tenant_id", "task_key"])
            .returning(col(PendingDispatch.id), col(PendingDispatch.task_key))
        ).all()
    }
    missing = [index for index, key in enumerate(keys) if key not in inserted]
    if missing:
        existing = {
            row.task_key: row
            for row in session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    col(PendingDispatch.task_key).in_([keys[i] for i in missing]),
                )
            ).all()
        }
        for index in missing:
            task_name, task_key, _, due = entries[index]
            found = existing[task_key]
            if (found.task_name, found.actor_id) != (
                task_name,
                context.actor_id,
            ) or (found.payload_digest or _payload_digest(found.payload)) != rows[
                index
            ]["payload_digest"]:
                raise DomainError("dispatch_key_conflict", "同一任务标识的配置不一致")
            # 与单条写入后由调用方改到期时间一致：复用已有消息也采用本次指定的到期。
            if due is not None:
                found.available_at = due
            inserted[task_key] = found.id
    return [inserted[key] for key in keys]


def compact_published_dispatches(
    session: Session,
    *,
    limit: int = 1000,
    retention_days: int = 7,
    sync_cursors: dict[str, list[str]] | None = None,
) -> int:
    """压缩已结算且没有活跃业务所有者的构建投递，保留幂等和审计身份。"""
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("Dispatch compaction limit must be between 1 and 5000")
    if type(retention_days) is not int or retention_days < 7:
        raise ValueError("Dispatch retention must be at least seven days")
    from app.modules.sync_control.retention import compact_sync_dispatches

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    sync_compacted = compact_sync_dispatches(
        session, limit=max(1, limit // 2), cutoff=cutoff, cursors=sync_cursors
    )
    if sync_compacted == limit:
        return sync_compacted
    # 延迟导入避免 outbox 与构建模型在应用启动时形成环；只有会在结算后清空
    # dispatch_id 的两类高频消息可压缩，素材核查消息仍保留完整恢复参数。
    from app.modules.builds.execution_models import ExecutionStep, SubmissionUnit

    step_owned = (
        select(ExecutionStep.id)
        .where(ExecutionStep.dispatch_id == PendingDispatch.id)
        .exists()
    )
    unit_owned = (
        select(SubmissionUnit.unit_id)
        .where(SubmissionUnit.dispatch_id == PendingDispatch.id)
        .exists()
    )
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    rows = session.exec(
        select(PendingDispatch)
        .where(
            col(PendingDispatch.published_at).is_not(None),
            col(PendingDispatch.published_at) <= cutoff,
            col(PendingDispatch.compacted_at).is_(None),
            col(PendingDispatch.task_name).in_(_COMPACTABLE_TASKS),
            or_(
                and_(
                    col(PendingDispatch.task_name) == "builds.execute_step", ~step_owned
                ),
                and_(
                    col(PendingDispatch.task_name) == "builds.execute_unit", ~unit_owned
                ),
            ),
        )
        .order_by(col(PendingDispatch.published_at), col(PendingDispatch.id))
        .limit(limit - sync_compacted)
        .with_for_update(skip_locked=True)
    ).all()
    now = datetime.now(UTC)
    for row in rows:
        row.payload_digest = row.payload_digest or _payload_digest(row.payload)
        row.payload = {}
        row.compacted_at = now
    session.flush()
    return sync_compacted + len(rows)


def _candidate_tenants(
    session: Session, *, now: datetime, page_size: int
) -> Iterator[UUID]:
    due = (
        select(col(PendingDispatch.id))
        .where(
            PendingDispatch.tenant_id == col(DispatchTenantCursor.tenant_id),
            col(PendingDispatch.published_at).is_(None),
            col(PendingDispatch.superseded_at).is_(None),
            col(PendingDispatch.retired_at).is_(None),
            or_(
                col(PendingDispatch.valid_until).is_(None),
                col(PendingDispatch.valid_until) > now,
            ),
            col(PendingDispatch.available_at) <= now,
        )
        .exists()
    )
    checked: set[UUID] = set()
    # 正常负载只取一页；跳锁后仍有发送预算时才补页。最多 10 页/1000 租户，
    # 防止竞争下无界扫描；每轮仍由外层限制 100 次实际尝试、每租户 5 条。
    for _ in range(10):
        candidates = session.exec(
            select(DispatchTenantCursor.tenant_id)
            .where(due, col(DispatchTenantCursor.tenant_id).not_in(checked))
            .order_by(
                col(DispatchTenantCursor.last_published_at).asc().nulls_first(),
                col(DispatchTenantCursor.tenant_id),
            )
            .limit(page_size)
        ).all()
        for tenant_id in candidates:
            checked.add(tenant_id)
            yield tenant_id
        if len(candidates) < page_size:
            return


def flush_dispatch(limit: int = 100) -> int:
    """One tenant round: at most one expansion, then ordinary FIFO; <=5/tenant.

    With only expansions due we deliberately leave the other slots unused.
    A second expansion cannot starve ordinary work as new requests arrive.
    Broker failures retain their original delivery IDs and normal backoff.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Dispatch limit must be a positive integer")
    limit = min(limit, 100)
    from app.jobs.queued_dispatches import queued_dispatches

    # 批量 broker 读取在数据库事务之外；命中后仍逐条实时确认原消息存在。
    started = monotonic()
    queued = queued_dispatches()
    database_started = monotonic()
    snapshot_seconds = database_started - started
    broker_seconds = 0.0
    now = datetime.now(UTC)
    published = 0
    attempted = 0
    with Session(engine) as session, session.begin():
        # 候选页保留公平顺序；只在实际服务前按主键领取锁，不占住未服务租户。
        for tenant_id in _candidate_tenants(session, now=now, page_size=limit):
            cursor = session.exec(
                select(DispatchTenantCursor)
                .where(DispatchTenantCursor.tenant_id == tenant_id)
                .with_for_update(skip_locked=True)
            ).first()
            if cursor is None:
                continue
            # 候选快照之后其他发布器可能已服务该租户；持锁后重新读取到期任务。
            slots = min(5, limit - attempted)
            pending = (
                select(PendingDispatch)
                .where(
                    PendingDispatch.tenant_id == cursor.tenant_id,
                    col(PendingDispatch.published_at).is_(None),
                    col(PendingDispatch.superseded_at).is_(None),
                    col(PendingDispatch.retired_at).is_(None),
                    or_(
                        col(PendingDispatch.valid_until).is_(None),
                        col(PendingDispatch.valid_until) > now,
                    ),
                    col(PendingDispatch.available_at) <= now,
                )
                .order_by(col(PendingDispatch.available_at), col(PendingDispatch.id))
                .with_for_update(skip_locked=True)
            )
            records = list(
                session.exec(
                    # A fixed literal preserves the partial-index proof when
                    # PostgreSQL switches a prepared query to a generic plan.
                    pending.where(
                        PendingDispatch.task_name
                        == literal(_EXPANSION_TASK, literal_execute=True)
                    ).limit(1)
                ).all()
            )
            ordinary_slots = slots - len(records)
            if ordinary_slots:
                records.extend(
                    session.exec(
                        pending.where(
                            PendingDispatch.task_name != _EXPANSION_TASK
                        ).limit(ordinary_slots)
                    ).all()
                )
            for record in records:
                attempted += 1
                broker_started = monotonic()
                try:
                    queue = dispatch_queue(record.task_name)
                    payload = validate_dispatch_payload(record.payload)
                    kwargs = {
                        "tenant_id": str(record.tenant_id),
                        "actor_id": str(record.actor_id),
                        "payload": payload,
                    }
                    from app.modules.sync_control.dispatch_envelope import (
                        dispatch_envelope,
                    )

                    envelope = dispatch_envelope(record)
                    if envelope is not None:
                        kwargs["dispatch"] = envelope
                    if not queued.contains(
                        queue=queue,
                        name=record.task_name,
                        task_id=str(record.id),
                        kwargs=kwargs,
                    ):
                        record.attempts += 1
                        celery_app.send_task(
                            record.task_name,
                            kwargs=kwargs,
                            task_id=str(record.id),
                            queue=queue,
                            **(
                                {"expires": record.valid_until}
                                if envelope is not None
                                else {}
                            ),
                            headers={
                                "diagnostic_due_at": record.available_at.timestamp()
                            },
                            # Redis 0最高：短投递tick优先于builds业务积压；报表纯补位
                            # 仍让出control。只改消费优先级，不改持久公平次序和正文。
                            **(
                                {"priority": 3}
                                if queue == "builds"
                                or (
                                    record.task_name == "reporting.plan_due"
                                    and payload == {"dispatch_only": True}
                                )
                                else {}
                            ),
                        )
                except Exception:
                    # Do not persist broker exceptions: they can contain secrets.
                    record.available_at = datetime.now(UTC) + timedelta(
                        seconds=min(300, 2 ** min(record.attempts, 8))
                    )
                else:
                    # 确认 broker 已持有相同投递也完成本轮 outbox；不新增队列副本。
                    record.published_at = datetime.now(UTC)
                    published += 1
                finally:
                    broker_seconds += monotonic() - broker_started
            if records:
                # Serviced attempts advance the round even during broker outages.
                cursor.last_published_at = datetime.now(UTC)
            if attempted >= limit:
                # 在请求下一候选前停止，普通满额轮次不能额外查询第二页。
                break
        commit_started = monotonic()
    finished = monotonic()
    # 只诊断成功返回的慢轮；不增加查询或改变发送预算，也不记录任务身份及正文。
    if finished - started > 2.0:
        logger.warning(
            "dispatch_flush_slow %s",
            json.dumps(
                {
                    "total_seconds": round(finished - started, 6),
                    "broker_snapshot_seconds": round(snapshot_seconds, 6),
                    "db_query_claim_seconds": round(
                        commit_started - database_started - broker_seconds, 6
                    ),
                    "broker_contains_send_seconds": round(broker_seconds, 6),
                    "db_commit_seconds": round(finished - commit_started, 6),
                    "attempted": attempted,
                    "published": published,
                }
            ),
        )
    return published
