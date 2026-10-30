"""匿名查询阶段观测；计时包含子阶段，不读取数据库，也不记录请求正文。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from functools import wraps
from time import monotonic
from types import TracebackType
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar, cast

if TYPE_CHECKING:
    from app.core.context import TenantContext
    from app.modules.reporting.schemas import ReportingFilter

P = ParamSpec("P")
R = TypeVar("R")


class _TracedPage(Protocol):
    @property
    def items(self) -> tuple[Any, ...]: ...

    @property
    def total(self) -> int: ...


logger = logging.getLogger("app.query_trace")
_FINGERPRINT_KEY = secrets.token_bytes(32)
_DIMENSIONS = frozenset({"account", "campaign", "adgroup", "ad", "material", "drama"})
_STAGES = frozenset(
    {
        "queue",
        "pool_wait",
        "authorize",
        "dependencies",
        "reuse_wait",
        "snapshot",
        "aggregate",
        "snapshot_write",
        "hydrate",
        "serialize",
        "page",
    }
)
_COUNTS = frozenset(
    {
        "account_count",
        "date_count",
        "matched_rows",
        "page_rows",
        "material_posts",
        "sql_count",
        "reused",
        "waiters",
    }
)
_CURRENT: ContextVar[QueryTrace | None] = ContextVar("query_trace", default=None)


def _outcome(error: BaseException | None) -> str:
    if error is None:
        return "success"
    cancelled = (
        isinstance(error, asyncio.CancelledError)
        or getattr(error, "status_code", None) == 499
        or getattr(getattr(error, "orig", None), "sqlstate", None) == "57014"
    )
    return "cancelled" if cancelled else "error"


class QueryTrace:
    """只接受固定阶段和计数名，防止动态身份或异常正文进入公共日志。

    身份经过进程随机密钥 HMAC；同进程可比较重复查询，日志持有者不能枚举账户
    反推身份。独立 logger 不继承 runtime_trace 的业务身份上下文字段。
    """

    def __init__(
        self,
        *,
        scope: Mapping[str, Any],
        dimension: str,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        payload = json.dumps(scope, sort_keys=True, separators=(",", ":"), default=str)
        self.fingerprint = hmac.new(
            _FINGERPRINT_KEY, payload.encode(), hashlib.sha256
        ).hexdigest()
        self.dimension = dimension if dimension in _DIMENSIONS else None
        self._clock = clock
        self._counts: dict[str, int] = {}
        self._stages: list[dict[str, Any]] = []
        self._parents: list[str] = []
        self._started: float | None = None
        self._token: Token[QueryTrace | None] | None = None

    def __enter__(self) -> QueryTrace:
        self._started = self._clock()
        self._token = _CURRENT.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._started is not None
        if self._token is not None:
            _CURRENT.reset(self._token)
        payload = {
            "event": "report_query",
            "fingerprint": self.fingerprint,
            "dimension": self.dimension,
            "outcome": _outcome(exc),
            "duration_ms": round((self._clock() - self._started) * 1000, 3),
            "timing_semantics": "inclusive",
            "stages": self._stages,
            "counts": self._counts,
        }
        try:
            logger.info(json.dumps(payload, separators=(",", ":")))
        except Exception:
            # 观测输出失败不能改变业务结果或遮盖原始异常。
            pass

    def count(self, name: str, value: int) -> None:
        if name in _COUNTS and type(value) is int and value >= 0:
            self._counts[name] = self._counts.get(name, 0) + value

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        if name not in _STAGES:
            yield
            return
        started = self._clock()
        parent = self._parents[-1] if self._parents else None
        self._parents.append(name)
        outcome = "success"
        try:
            yield
        except BaseException as error:
            outcome = _outcome(error)
            raise
        finally:
            self._parents.pop()
            self._stages.append(
                {
                    "name": name,
                    "parent": parent,
                    "outcome": outcome,
                    "duration_ms": round((self._clock() - started) * 1000, 3),
                }
            )


@contextmanager
def stage(name: str) -> Iterator[None]:
    trace = _CURRENT.get()
    if trace is None:
        yield
    else:
        with trace.stage(name):
            yield


def count(name: str, value: int) -> None:
    trace = _CURRENT.get()
    if trace is not None:
        trace.count(name, value)


def timed(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with stage(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def trace_query[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        filters = cast("ReportingFilter", kwargs["filters"])
        context = cast("TenantContext", kwargs["context"])
        # 原始参数仅进入HMAC，不被日志保留；游标及Token不参与指纹。
        with QueryTrace(
            scope={
                "tenant": context.tenant_id,
                "actor": context.actor_id,
                "bc": kwargs["bc_id"],
                "filters": filters.model_dump(mode="json"),
            },
            dimension=filters.dimension,
        ) as trace:
            trace.count("date_count", (filters.end_date - filters.start_date).days + 1)
            result = function(*args, **kwargs)
            page = cast(_TracedPage, result)
            trace.count("page_rows", len(page.items))
            trace.count("matched_rows", page.total)
            return result

    return wrapped
