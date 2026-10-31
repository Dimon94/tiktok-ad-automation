"""MCP 回调的短数据库事务和逐命令 Redis 期限；不修改应用共享连接池。"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from redis import ConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from app.core.errors import DomainError

MAX_RESOURCE_SECONDS = 5.0
logger = logging.getLogger(__name__)


def _remaining(task_deadline: datetime) -> float:
    if task_deadline.tzinfo is None or task_deadline.utcoffset() is None:
        raise DomainError("tiktok_call_deadline_exceeded", "本次调用期限无效或已结束")
    remaining = (task_deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise DomainError("tiktok_call_deadline_exceeded", "本次调用期限无效或已结束")
    return remaining


def _resource_error() -> DomainError:
    return DomainError(
        "tiktok_local_resources_unavailable", "调用前资源暂不可用", retryable=True
    )


@contextmanager
def bounded_session(
    database_engine: Engine,
    *,
    task_deadline: datetime,
    max_resource_seconds: float = MAX_RESOURCE_SECONDS,
) -> Iterator[Session]:
    """默认 5 秒短事务；只有明确的批量本地登记才能传入更长的资源上限。"""
    remaining = _remaining(task_deadline)
    # 固定 psycopg 驱动把小于 2 秒的连接期限提高到 2 秒；预算不足时不连接。
    if remaining < 2:
        raise DomainError("tiktok_call_deadline_exceeded", "本次调用剩余期限不足")
    local_deadline = min(
        task_deadline, datetime.now(UTC) + timedelta(seconds=max_resource_seconds)
    )
    milliseconds = max(1, int(_remaining(local_deadline) * 1000))
    scoped = create_engine(
        database_engine.url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": min(5, max(2, int(remaining))),
            "options": (
                f"-c statement_timeout={milliseconds} "
                f"-c lock_timeout={milliseconds} "
                f"-c idle_in_transaction_session_timeout={milliseconds}"
            ),
        },
    )

    @event.listens_for(scoped, "before_cursor_execute")
    def limit_statement(
        _connection: Any,
        cursor: Any,
        _statement: Any,
        _parameters: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        # 多次权限查询共享同一期限，不在每条 SQL 上重新给完整预算。
        limit = max(1, int(_remaining(local_deadline) * 1000))
        cursor.execute(f"SET LOCAL statement_timeout = {limit}")
        cursor.execute(f"SET LOCAL lock_timeout = {limit}")

    failed = False
    try:
        with Session(scoped) as session:
            yield session
    except SQLAlchemyError as error:
        failed = True
        logger.exception("bounded database session failed: %s", type(error).__name__)
    finally:
        scoped.dispose()
    if failed:
        # SQL/连接异常可能携带密文参数和 DSN；在原捕获块外返回固定错误。
        raise _resource_error() from None


class _DeadlineRedis(Redis):
    def __init__(self, source: Redis, *, task_deadline: datetime):
        self._task_deadline = task_deadline
        self._source_class = source.connection_pool.connection_class
        self._source_kwargs = dict(source.connection_pool.connection_kwargs)
        # 继承 Redis 命令序列化；每条实际命令使用下面独占的短连接。
        super().__init__(connection_pool=self._new_pool())

    def _new_pool(self) -> ConnectionPool:
        timeout = min(MAX_RESOURCE_SECONDS, _remaining(self._task_deadline))
        options: dict[str, Any] = {
            **self._source_kwargs,
            "socket_connect_timeout": timeout,
            "socket_timeout": timeout,
            "retry": Retry(NoBackoff(), 0),
            "retry_on_error": [],
            "health_check_interval": 0,
        }
        return ConnectionPool(
            connection_class=self._source_class, max_connections=1, **options
        )

    def execute_command(self, *args: Any, **options: Any) -> Any:
        failed = False
        try:
            with Redis.from_pool(self._new_pool()) as client:
                result = client.execute_command(*args, **options)
        except RedisError:
            failed = True
        if failed:
            raise _resource_error()
        return result

    @contextmanager
    def cleanup_scope(self) -> Iterator[Redis]:
        # 请求期限结束不应阻止释放自己的在途租约。清理只移除已持有的lease，
        # 使用独立的5秒资源期限，不恢复请求发送或返还已消耗的平台速率。
        with bounded_redis(
            self,
            task_deadline=datetime.now(UTC) + timedelta(seconds=MAX_RESOURCE_SECONDS),
        ) as client:
            yield client

    def pipeline(self, transaction: bool = True, shard_hint: Any = None) -> Any:
        # 准入使用单次 Lua 原子命令；延迟 pipeline 会绕过逐命令期限检查。
        raise DomainError(
            "tiktok_local_resources_unavailable", "调用前不支持批量缓存命令"
        )


@contextmanager
def bounded_redis(redis_client: Redis, *, task_deadline: datetime) -> Iterator[Redis]:
    _remaining(task_deadline)
    client = _DeadlineRedis(redis_client, task_deadline=task_deadline)
    try:
        yield client
    finally:
        client.close()
        client.connection_pool.disconnect()
