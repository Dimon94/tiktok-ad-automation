"""Known dispatch tasks. Registration is an application startup operation.

P02+ modules register_dispatch_task(name, queue) and define their Celery handler
with keyword arguments tenant_id, actor_id, payload. Every business handler must
parse its own payload and re-query current permissions using P02 require_tenant;
a role from a request or queue message never constitutes authorization.
"""

from time import monotonic
from typing import Any
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy.orm import Session as SQLAlchemySession

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs import diagnostics as _diagnostics  # noqa: F401 - 注册任务诊断信号
from app.jobs.celery_app import celery_app

_ALLOWED_QUEUES = frozenset(
    {
        "control",
        "resources",
        "resource-results",
        "builds",
        "ads-directory",
        "ads-publish",
        "ads-reporting",
        "ads-backfill",
        "ads-live-directory",
        "ad-management",
    }
)
_RESOURCE_RESULTS = frozenset(
    {
        "materials.verify_target",
        "materials.verify_original",
        "materials.verify_cover",
    }
)
_DISPATCH_TASKS: dict[str, str] = {}


def register_dispatch_task(name: str, queue: str) -> None:
    """Register an explicit task-to-queue mapping; conflicting mappings fail."""
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or name == "jobs.flush_dispatch"
    ):
        raise ValueError("Invalid business dispatch task name")
    # 结果核实独立于准备；封面准备可能发生慢上传，不能占用结果专用执行槽。
    if queue == "resources" and name in _RESOURCE_RESULTS:
        queue = "resource-results"
    if queue not in _ALLOWED_QUEUES:
        raise ValueError("Unknown dispatch queue")
    if name in _DISPATCH_TASKS and _DISPATCH_TASKS[name] != queue:
        raise ValueError("Dispatch task already registered with another queue")
    _DISPATCH_TASKS[name] = queue


def guard_sync_dispatch(
    *,
    database_engine: Any,
    task_name: str,
    tenant_id: str,
    actor_id: str,
    payload: dict[str, Any],
    dispatch: dict[str, Any] | None,
) -> bool:
    """已注册同步入口共用寿命围栏；不改变legacy及广告写任务注册合同。"""
    from app.modules.sync_control.dispatch_envelope import (
        SYNC_EXECUTION_TASKS,
        consume_sync_dispatch,
    )

    dispatch_queue(task_name)
    if task_name not in SYNC_EXECUTION_TASKS and task_name not in {
        "reporting.plan_due",
        "reporting.plan_continue",
    }:
        raise ValueError("not a canonical sync task")
    return consume_sync_dispatch(
        database_engine=database_engine,
        task_name=task_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
        dispatch=dispatch,
    )


def dispatch_queue(name: str) -> str:
    try:
        return _DISPATCH_TASKS[name]
    except KeyError, TypeError:
        raise DomainError("dispatch_task_not_allowed", "任务未注册") from None


register_dispatch_task("jobs.probe", "control")
register_dispatch_task("accounts.refresh_mcp", "control")
register_dispatch_task("accounts.runtime_discover", "resources")
register_dispatch_task("builds.historical_read_step", "resources")
# 导出只消费本地冻结快照，和报表同步共用 ads-reporting worker 隔离。
register_dispatch_task("reporting.export", "ads-reporting")
register_dispatch_task("reporting.plan_query_pages", "control")
register_dispatch_task("reporting.refresh_query_page", "ads-reporting")
register_dispatch_task("ad_management.execute", "ad-management")


@celery_app.task(name="jobs.probe")  # type: ignore[untyped-decorator]
def probe(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> None:
    """Internal no-op diagnostics only; does not grant or verify business access."""
    UUID(tenant_id)
    UUID(actor_id)
    from app.jobs.outbox import validate_dispatch_payload

    validate_dispatch_payload(payload)


def drain_dispatch(limit: int = 100) -> int:
    """Drain bounded fair rounds, committing each before considering another.

    A time budget is checked between complete rounds, never inside a transaction.
    No progress ends this delivery; broker backoff remains with its original row.
    These are local queue messages, not permissions to make external API calls.
    """
    from app.jobs.outbox import flush_dispatch

    deadline = monotonic() + settings.DISPATCH_TIME_BUDGET_SECONDS
    published = 0
    for _ in range(settings.DISPATCH_MAX_ROUNDS):
        count = flush_dispatch(limit=limit)
        published += count
        if count == 0 or monotonic() >= deadline:
            break
    return published


@celery_app.task(name="jobs.flush_dispatch", time_limit=30, soft_time_limit=25)  # type: ignore[untyped-decorator]
def flush_dispatch_task(limit: int = 100) -> int:
    return drain_dispatch(limit=limit)


@celery_app.task(name="jobs.compact_dispatches", time_limit=45, soft_time_limit=40)  # type: ignore[untyped-decorator]
def compact_dispatches_task(limit: int = 1000) -> int:
    """分批压缩已结算构建/同步消息；保留七天正文及幂等摘要。"""
    import json
    from datetime import UTC, datetime

    from redis import Redis
    from sqlalchemy import text
    from sqlmodel import Session

    from app.jobs.outbox import compact_published_dispatches
    from app.modules.sync_control.retention import prune_sync_dispatches

    with Redis.from_url(settings.REDIS_URL) as redis:
        key = "sync:legacy-dispatch-compaction:cursor:v1"
        cursors = json.loads(redis.get(key) or "{}")
        with Session(engine) as session, session.begin():
            typing_cast(SQLAlchemySession, session).execute(
                text("SET LOCAL statement_timeout='5s'")
            )
            typing_cast(SQLAlchemySession, session).execute(
                text("SET LOCAL lock_timeout='200ms'")
            )
            pruned = prune_sync_dispatches(
                session, now=datetime.now(UTC), limit=max(1, limit // 2)
            )
            compacted = compact_published_dispatches(
                session, limit=max(1, limit // 2), sync_cursors=cursors
            )
        # 只有数据库提交后才推进扫描位置；丢失游标仅导致安全重扫，不能跳过清理。
        redis.set(key, json.dumps(cursors), ex=86400)
        return compacted + sum(pruned.values())
