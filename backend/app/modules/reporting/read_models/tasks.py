"""派生模型独立维护：不占平台额度，每桶单独提交并保留后继dirty。"""

from collections.abc import Callable
from time import monotonic
from typing import Protocol, cast

from sqlmodel import Session

from app.core.db import engine
from app.jobs.celery_app import celery_app
from app.modules.reporting.query_reuse import prune_query_reuse
from app.modules.reporting.read_models.projector import rebuild_pending


class ProjectionTask(Protocol):
    def __call__(self) -> int: ...

    def run(self) -> int: ...


@cast(
    Callable[[Callable[[], int]], ProjectionTask],
    celery_app.task(
        name="reporting.project_read_models", soft_time_limit=30, time_limit=40
    ),
)
def project_read_models() -> int:
    deadline = monotonic() + 4
    completed = 0
    with Session(engine) as session:
        prune_query_reuse(session)
        session.commit()
    # 由墙钟预算让出publisher，不再因25个轻量桶提前结束并空等下一次Beat。
    # 仍逐桶提交，后续失败不撤销已完成桶，也不扩大单事务锁范围。
    while monotonic() < deadline:
        with Session(engine) as session:
            results = rebuild_pending(session, limit=1)
            session.commit()
        if not results:
            break
        completed += len(results)
    return completed
