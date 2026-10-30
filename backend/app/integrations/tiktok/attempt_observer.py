"""只观察SDK已有物理发送；不实现HTTP、不修改平台准入账本。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol


class AttemptObserver(Protocol):
    def attempt(self, endpoint: str) -> None: ...
    def flush(self) -> None: ...


_CURRENT: ContextVar[AttemptObserver | None] = ContextVar(
    "sync_attempt_observer", default=None
)


@contextmanager
def bind_attempt_observer(observer: AttemptObserver) -> Iterator[None]:
    token = _CURRENT.set(observer)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_attempt_observer() -> AttemptObserver | None:
    return _CURRENT.get()


@contextmanager
def physical_attempt_scope(client: Any, endpoint: str) -> Iterator[None]:
    observer = _CURRENT.get()
    if observer is None:
        yield
        return
    prepare = getattr(observer, "prepare", None)
    if prepare is not None:
        prepare(endpoint)
    missing = object()
    previous = getattr(client, "_sync_attempt_observer", missing)
    pool = client.rest_client.pool_manager
    previous_pool = getattr(pool, "_sync_attempt_observer", missing)
    # 官方SDK通过线程池调用request；ContextVar不会自动跨线程，因此在本次
    # 私有client上捕获观察器。离开作用域立即恢复，不污染下一次调用。
    client._sync_attempt_observer = pool._sync_attempt_observer = (observer, endpoint)
    try:
        yield
    finally:
        if previous is missing:
            del client._sync_attempt_observer
        else:
            client._sync_attempt_observer = previous
        if previous_pool is missing:
            del pool._sync_attempt_observer
        else:
            pool._sync_attempt_observer = previous_pool
        observer.flush()
