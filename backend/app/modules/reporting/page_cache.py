"""首屏完整结果的一分钟缓存；授权实时检查，耗时构建由报表Worker完成。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import time
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import Connection, event
from sqlmodel import Session

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.modules.accounts.models import TenantBC
from app.modules.reporting.filters import compile_filter
from app.modules.reporting.queries import _build_snapshot, _read_page, query_ads
from app.modules.reporting.query_models import is_transaction_cleanup, read_snapshot
from app.modules.reporting.query_reuse import (
    QueryCancellation,
    authority_stamp,
    query_key,
)
from app.modules.reporting.schemas import AdsQueryPage, ReportingFilter
from app.modules.tenants.permissions import require_tenant

logger = logging.getLogger(__name__)
PREFIX = "report-page:v1"
CACHE_SECONDS = 60
ACTIVE_SECONDS = 180
LEASE_SECONDS = 120
BUILD_SECONDS = 90
BUILD_SLOTS = 2
RELEASE = """if redis.call('get',KEYS[1])==ARGV[1] then
    return redis.call('del',KEYS[1]) end return 0"""
PUBLISH = """if redis.call('get',KEYS[1])==ARGV[1] and redis.call('exists',KEYS[3])==1 then
    redis.call('set',KEYS[2],ARGV[2],'EX',ARGV[3]); return 1 end return 0"""


def client() -> Redis:
    return Redis.from_url(
        settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1
    )


def due_key() -> str:
    return PREFIX + ":due"


def watch_key(key: str) -> str:
    return key + ":watch"


def lease_key(key: str) -> str:
    return key + ":lease"


def value(value: Any) -> str | None:
    return value.decode() if isinstance(value, bytes) else cast(str | None, value)


def scope_key(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    limit: int,
) -> str:
    compile_filter(filters)
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise HTTPException(404, detail="query_scope_not_found")
    stamp = authority_stamp(session, context=context, bc_id=bc_id, filters=filters)
    digest = hashlib.sha256(
        f"{query_key(context, bc_id, filters)}:{limit}:{stamp.scope_digest}:{stamp.permission_revision}".encode()
    ).hexdigest()
    return PREFIX + ":page:" + digest


def watch(
    redis: Redis,
    key: str,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    limit: int,
    force: bool = False,
) -> None:
    descriptor = {
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id),
        "bc_id": bc_id,
        "filters": filters.model_dump(mode="json"),
        "limit": limit,
    }
    if force:
        redis.delete(key + ":error")
    redis.set(watch_key(key), json.dumps(descriptor), ex=ACTIVE_SECONDS)
    # 普通命中不推迟已安排的刷新；手动刷新把本键提前到下一次Control扫描。
    redis.zadd(due_key(), {key: time.time()}, nx=not force)


def cached(redis: Redis, key: str) -> AdsQueryPage | None:
    raw = redis.get(key)
    if raw is None:
        return None
    try:
        page = AdsQueryPage.model_validate_json(
            zlib.decompress(base64.b64decode(cast(bytes, raw), validate=True))
        )
        if page.cache_expires_at and page.cache_expires_at > datetime.now(UTC):
            return page
    except zlib.error, binascii.Error, ValidationError, TypeError:
        pass
    return None


def serialize_page(page: AdsQueryPage) -> str:
    return base64.b64encode(
        zlib.compress(page.model_dump_json().encode(), level=3)
    ).decode("ascii")


def query_cached_ads(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    snapshot_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
    cancellation: QueryCancellation | None = None,
    force_refresh: bool = False,
    wait_seconds: float = 50,
) -> AdsQueryPage:
    # 私有/测试事务可能含未提交事实，不能把它们发布到跨请求缓存。
    if (
        snapshot_id is not None
        or cursor is not None
        or session.get_bind() is not engine
    ):
        return query_ads(
            session,
            context=context,
            bc_id=bc_id,
            filters=filters,
            snapshot_id=snapshot_id,
            cursor=cursor,
            limit=limit,
            cancellation=cancellation,
        )
    if not 1 <= limit <= 100:
        raise HTTPException(422, detail="invalid_snapshot_page")
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("page cache requires a read-only request session")
    try:
        key = scope_key(
            session, context=context, bc_id=bc_id, filters=filters, limit=limit
        )
    except ValueError as error:
        raise HTTPException(422, detail="invalid_reporting_filter") from error
    session.rollback()
    try:
        with client() as redis:
            first = cached(redis, key)
            baseline = first.snapshot.snapshot_id if first and force_refresh else None
            watch(
                redis,
                key,
                context=context,
                bc_id=bc_id,
                filters=filters,
                limit=limit,
                force=force_refresh,
            )
            deadline = time.monotonic() + wait_seconds
            next_authority = time.monotonic()
            while True:
                if cancellation and cancellation.event.is_set():
                    raise HTTPException(499, detail="query_cancelled")
                page = cached(redis, key)
                if page and (baseline is None or page.snapshot.snapshot_id != baseline):
                    snapshot = read_snapshot(
                        session,
                        context=context,
                        bc_id=bc_id,
                        snapshot_id=page.snapshot.snapshot_id,
                    )
                    if (
                        snapshot.filters != filters.model_dump(mode="json")
                        or page.snapshot.filters != filters
                    ):
                        raise HTTPException(
                            409, detail="query_snapshot_filter_mismatch"
                        )
                    return page.model_copy(
                        update={"cache_hit": first is not None and not force_refresh}
                    )
                if redis.exists(key + ":error"):
                    raise HTTPException(503, detail="query_build_failed")
                if time.monotonic() >= deadline:
                    raise HTTPException(503, detail="query_build_busy")
                # 等待新一代时也及时发现撤权；两轮之间不持有数据库连接。
                if time.monotonic() >= next_authority:
                    current = scope_key(
                        session,
                        context=context,
                        bc_id=bc_id,
                        filters=filters,
                        limit=limit,
                    )
                    session.rollback()
                    if current != key:
                        raise HTTPException(403, detail="report_scope_forbidden")
                    next_authority = time.monotonic() + 1
                time.sleep(0.1)
    except RedisError:
        # 缓存不可用时仍允许精确查询，不返回旧数据或给失效缓存续期。
        return query_ads(
            session,
            context=context,
            bc_id=bc_id,
            filters=filters,
            limit=limit,
            cancellation=cancellation,
        )


def acquire_slot(redis: Redis, token: str) -> str | None:
    for slot in range(BUILD_SLOTS):
        key = f"{PREFIX}:slot:{slot}"
        if redis.set(key, token, nx=True, ex=LEASE_SECONDS):
            return key
    return None


def refresh_page(redis: Redis, key: str, token: str) -> bool:
    """令牌去重与两个全局槽保护数据库；RR提交后才发布完整页。"""
    if (
        not key.startswith(PREFIX + ":page:")
        or value(redis.get(lease_key(key))) != token
    ):
        return False
    slot = acquire_slot(redis, token)
    published = False
    try:
        if slot is None:
            return False
        raw = redis.get(watch_key(key))
        if raw is None:
            return False
        descriptor = json.loads(cast(str, value(raw)))
        filters = ReportingFilter.model_validate(descriptor["filters"])
        limit = int(descriptor["limit"])
        if not 1 <= limit <= 100:
            return False
        with Session(engine) as session:
            context = require_tenant(
                session,
                tenant_id=UUID(descriptor["tenant_id"]),
                actor_id=UUID(descriptor["actor_id"]),
                action="read",
            )
            expected = scope_key(
                session,
                context=context,
                bc_id=descriptor["bc_id"],
                filters=filters,
                limit=limit,
            )
        if expected != key:
            redis.delete(watch_key(key))
            redis.zrem(due_key(), key)
            return False
        with engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with (
                connection.begin(),
                Session(bind=connection, expire_on_commit=False) as session,
            ):
                view_clock = time.monotonic()
                connection.exec_driver_sql("SET LOCAL statement_timeout='90s'")
                connection.exec_driver_sql(
                    "SET LOCAL idle_in_transaction_session_timeout='30s'"
                )
                connection.exec_driver_sql("SET LOCAL jit=off")
                started = connection.exec_driver_sql(
                    "SELECT transaction_timestamp()"
                ).scalar_one()
                deadline = view_clock + BUILD_SECONDS

                def budget(
                    _connection: Connection,
                    cursor: Any,
                    statement: str,
                    _parameters: Any,
                    _context: Any,
                    _many: bool,
                ) -> None:
                    if is_transaction_cleanup(statement):
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise HTTPException(503, detail="query_build_busy")
                    cursor.execute(
                        "SELECT set_config('statement_timeout',%s,true)",
                        (f"{max(1, int(remaining * 1000))}ms",),
                    )

                event.listen(connection, "before_cursor_execute", budget)
                try:
                    snapshot = _build_snapshot(
                        session,
                        context=context,
                        bc_id=descriptor["bc_id"],
                        filters=filters,
                    )
                    page = _read_page(
                        session,
                        context=context,
                        bc_id=descriptor["bc_id"],
                        filters=filters,
                        snapshot=snapshot,
                        cursor=None,
                        limit=limit,
                    )
                    if time.monotonic() >= deadline:
                        raise HTTPException(503, detail="query_build_busy")
                    session.flush()
                finally:
                    event.remove(connection, "before_cursor_execute", budget)
        # 构建途中撤权/换绑也不能把旧权限结果发布到缓存。
        with Session(engine) as session:
            if (
                scope_key(
                    session,
                    context=context,
                    bc_id=descriptor["bc_id"],
                    filters=filters,
                    limit=limit,
                )
                != key
            ):
                return False
        # 单调时钟计入完整构建与复核耗时，避免DB与应用时钟偏差给缓存额外续期。
        now = datetime.now(UTC)
        seconds = int(
            min(
                CACHE_SECONDS - (time.monotonic() - view_clock),
                (page.snapshot.expires_at - now).total_seconds(),
            )
        )
        if seconds <= 0:
            return False
        expires = now + timedelta(seconds=seconds)
        page = page.model_copy(
            update={
                "cache_data_at": started,
                "cache_generated_at": now,
                "cache_expires_at": expires,
                "cache_hit": False,
            }
        )
        payload = serialize_page(page)
        published = bool(
            redis.eval(
                PUBLISH,
                3,
                lease_key(key),
                key,
                watch_key(key),
                token,
                payload,
                str(seconds),
            )
        )
        if published:
            redis.delete(key + ":error")
            redis.zadd(due_key(), {key: max(time.time(), expires.timestamp() - 45)})
        return published
    except HTTPException, DomainError:
        _publish_failure(redis, key, token)
        logger.info(
            "report_page_scope_or_build_unavailable", extra={"key_hash": key[-12:]}
        )
        return False
    except Exception as error:
        _publish_failure(redis, key, token)
        logger.warning(
            "report_page_refresh_failed",
            extra={"key_hash": key[-12:], "error_type": type(error).__name__},
        )
        return False
    finally:
        if slot:
            redis.eval(RELEASE, 1, slot, token)
        owned = redis.eval(RELEASE, 1, lease_key(key), token)
        if owned and not published and redis.exists(watch_key(key)):
            redis.zadd(due_key(), {key: time.time() + 5})


def _publish_failure(redis: Redis, key: str, token: str) -> None:
    """只发布仍持租约的失败，不存半结果；复用原一分钟缓存寿命与重查入口。"""
    redis.eval(
        """if redis.call('get',KEYS[1])==ARGV[1] then
        return redis.call('set',KEYS[2],'query_build_failed','EX',ARGV[2]) end return 0""",
        2,
        lease_key(key),
        key + ":error",
        token,
        str(CACHE_SECONDS),
    )
