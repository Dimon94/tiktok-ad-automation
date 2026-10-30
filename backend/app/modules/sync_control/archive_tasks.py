"""每轮至多一个终态运行/提交intent；Redis游标仅影响效率，不承载持久证据。"""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import UUID

from redis import Redis
from sqlalchemy import literal, tuple_
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db import engine
from app.jobs.celery_app import celery_app
from app.modules.sync_control.archive import (
    SessionFactory,
    archive_run,
    finalize_archive,
    retention_candidates,
)
from app.modules.sync_control.archive_storage import S3RunArchive
from app.modules.sync_control.models import SyncArchiveFinalization
from app.modules.tenants.models import Tenant

CURSOR_KEY = "sync:run-archive:cursor:v1"
LOCK_KEY = "sync:run-archive:lock:v1"
KINDS = ("report", "directory", "balance")
SCAN_STEPS = 8
SCAN_SECONDS = 5
ITEM_BACKOFF_SECONDS = 60
INTENT_SCAN_ROWS = 32


def _json_cursor(redis: Redis, key: str) -> dict[str, Any]:
    raw = redis.get(key)
    if not isinstance(raw, (str, bytes)):
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except TypeError, ValueError:
        return {}


def _item_backoff_key(tenant_id: UUID, kind: str, run_id: UUID, stage: str) -> str:
    return f"{CURSOR_KEY}:backoff:{stage}:{tenant_id}:{kind}:{run_id}"


def _defer_item(
    redis: Redis,
    *,
    tenant_id: UUID,
    kind: str,
    run_id: UUID,
    stage: str,
    error: Exception,
) -> dict[str, object]:
    # 退避只影响重试时机；失败源/intent仍在DB，Redis丢失只会提前再试。
    redis.set(
        _item_backoff_key(tenant_id, kind, run_id, stage),
        type(error).__name__,
        ex=ITEM_BACKOFF_SECONDS,
    )
    return {
        "status": "item_deferred",
        "stage": stage,
        "error_type": type(error).__name__,
        "tenant_id": str(tenant_id),
        "kind": kind,
        "run_id": str(run_id),
    }


def _finalize_one(
    session_factory: SessionFactory, storage: S3RunArchive, redis: Redis
) -> dict[str, object] | None:
    cursor_key = f"{CURSOR_KEY}:intent"
    cursor = _json_cursor(redis, cursor_key)
    with session_factory() as session:
        query = select(SyncArchiveFinalization).order_by(
            col(SyncArchiveFinalization.created_at),
            col(SyncArchiveFinalization.tenant_id),
            col(SyncArchiveFinalization.kind),
            col(SyncArchiveFinalization.execution_id),
        )
        if cursor:
            try:
                after = (
                    datetime.fromisoformat(cursor["at"]),
                    UUID(cursor["tenant"]),
                    str(cursor["kind"]),
                    UUID(cursor["id"]),
                )
            except KeyError, TypeError, ValueError:
                after = None
            if after:
                query = query.where(
                    tuple_(
                        col(SyncArchiveFinalization.created_at),
                        col(SyncArchiveFinalization.tenant_id),
                        col(SyncArchiveFinalization.kind),
                        col(SyncArchiveFinalization.execution_id),
                    )
                    > tuple_(*(literal(value) for value in after))
                )
        intents = list(session.exec(query.limit(INTENT_SCAN_ROWS)).all())
    if not intents:
        redis.delete(cursor_key)
        return None
    for intent in intents:
        cursor = {
            "at": intent.created_at.isoformat(),
            "tenant": str(intent.tenant_id),
            "kind": intent.kind,
            "id": str(intent.execution_id),
        }
        redis.set(cursor_key, json.dumps(cursor))
        if redis.exists(
            _item_backoff_key(
                intent.tenant_id, intent.kind, intent.execution_id, "finalization"
            )
        ):
            continue
        try:
            finalize_archive(
                session_factory,
                storage,
                tenant_id=intent.tenant_id,
                kind=intent.kind,
                run_id=intent.execution_id,
            )
        except Exception as error:
            return _defer_item(
                redis,
                tenant_id=intent.tenant_id,
                kind=intent.kind,
                run_id=intent.execution_id,
                stage="finalization",
                error=error,
            )
        return {"status": "finalized", "finalized": 1}
    return None


def archive_one(
    session_factory: SessionFactory,
    storage: S3RunArchive,
    redis: Redis,
    *,
    now: datetime,
) -> dict[str, object]:
    """先轮转恢复intent，再轮转租户；单项故障不阻止其他租户前进。"""
    finalized = _finalize_one(session_factory, storage, redis)
    if finalized is not None:
        return finalized
    raw = redis.get(CURSOR_KEY)
    if not isinstance(raw, (str, bytes)):
        raw = None
    try:
        pointer = json.loads(raw) if raw else {}
        after_tenant = (
            UUID(pointer["after_tenant"]) if pointer.get("after_tenant") else None
        )
        # 显式tenant用于管理员/测试从指定租户开始，不永久锁定这一户。
        selected = UUID(pointer["tenant"]) if pointer.get("tenant") else None
    except TypeError, ValueError, KeyError, AttributeError:
        after_tenant, selected = None, None
    deadline = monotonic() + SCAN_SECONDS
    wrapped = False
    tenant_id: UUID | None
    for _ in range(SCAN_STEPS):
        if monotonic() >= deadline:
            break
        with session_factory() as session:
            if selected is not None:
                tenant_id, selected = selected, None
            else:
                query = select(Tenant.id).order_by(col(Tenant.id)).limit(1)
                if after_tenant is not None:
                    query = query.where(Tenant.id > after_tenant)
                tenant_id = session.exec(query).first()
                if tenant_id is None:
                    if wrapped:
                        return {"status": "scan_complete", "finalized": 0}
                    after_tenant, wrapped = None, True
                    redis.set(CURSOR_KEY, "{}")
                    continue
            position_key = f"{CURSOR_KEY}:tenant:{tenant_id}"
            try:
                position_raw = redis.get(position_key)
                if not isinstance(position_raw, (str, bytes)):
                    position_raw = "{}"
                position: dict[str, Any] = json.loads(position_raw)
                index = int(position.get("kind", 0))
                kind = KINDS[index]
                after = (
                    (datetime.fromisoformat(position["time"]), UUID(position["id"]))
                    if position.get("time")
                    else None
                )
            except TypeError, ValueError, KeyError, IndexError:
                position, index, kind, after = {}, 0, KINDS[0], None
            candidates = retention_candidates(
                session, tenant_id=tenant_id, kind=kind, now=now, cursor=after, limit=1
            )
        after_tenant = tenant_id
        if candidates:
            finished, run_id = candidates[0]
            # 候选游标先推进；上传异常保留源并退避，不把其他租户锁在这一项后面。
            position = {"kind": index, "time": finished.isoformat(), "id": str(run_id)}
            redis.set(position_key, json.dumps(position), ex=30 * 86400)
            redis.set(CURSOR_KEY, json.dumps({"after_tenant": str(tenant_id)}))
            if redis.exists(_item_backoff_key(tenant_id, kind, run_id, "archive")):
                return {
                    "status": "item_deferred",
                    "stage": "archive",
                    "reason": "backoff",
                }
            try:
                result = archive_run(
                    session_factory,
                    storage,
                    tenant_id=tenant_id,
                    kind=kind,
                    run_id=run_id,
                    now=now,
                )
            except Exception as error:
                return _defer_item(
                    redis,
                    tenant_id=tenant_id,
                    kind=kind,
                    run_id=run_id,
                    stage="archive",
                    error=error,
                )
            return {"status": "processed", **asdict(result)}
        # 无候选也让下一租户获得一次机会；每户3种摘要独立轮换。
        redis.set(
            position_key, json.dumps({"kind": (index + 1) % len(KINDS)}), ex=30 * 86400
        )
        redis.set(CURSOR_KEY, json.dumps({"after_tenant": str(tenant_id)}))
    return {"status": "scan_budget", "finalized": 0}


def archive_batch(
    session_factory: SessionFactory,
    storage: S3RunArchive,
    redis: Redis,
    *,
    now: datetime,
    max_runs: int = 1,
    time_budget_seconds: float = 20,
) -> dict[str, object]:
    if not 1 <= max_runs <= 1000 or not 0 < time_budget_seconds <= 90:
        raise ValueError("invalid archive maintenance budget")
    deadline, processed, results = monotonic() + time_budget_seconds, 0, []
    while processed < max_runs and monotonic() < deadline:
        result = archive_one(session_factory, storage, redis, now=now)
        results.append(result)
        if result["status"] in {"processed", "finalized", "item_deferred"}:
            processed += 1
        else:
            break
        # 每次网络操作结束重新看剩余预算；不额外派发耗时归档阻塞下一租户。
    return {"status": "batch", "processed": processed, "results": results}


@celery_app.task(name="sync.archive_runs", time_limit=120, soft_time_limit=110)  # type: ignore[untyped-decorator]
def archive_runs_task(
    max_runs: int = 1, time_budget_seconds: float = 20
) -> dict[str, object]:
    try:
        storage = S3RunArchive.configured()
    except Exception:
        return {"status": "storage_unconfigured", "archived": False}
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        lock = redis.lock(LOCK_KEY, timeout=180, blocking=False)
        if not lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            return archive_batch(
                lambda: Session(engine),
                storage,
                redis,
                now=datetime.now(UTC),
                max_runs=max_runs,
                time_budget_seconds=time_budget_seconds,
            )
        except Exception as error:
            # 只记类型，底层S3错误可能包含桶或签名；源或intent始终可恢复。
            return {"status": "retry_pending", "error_type": type(error).__name__}
        finally:
            lock.release()
    finally:
        redis.close()
