"""场景独立资源的有界并行 HTTP；数据库和客户端不在线程间共享。"""

from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.scenes import ScenePage
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.read_normalization import scene_arguments
from app.jobs.admission import admission_policy

from .scene_job_models import SceneJob, SceneJobPage
from .scene_schemas import SceneResource

RESOURCES: tuple[SceneResource, ...] = ("identity", "minis", "cta", "vbo", "regions")
REQUEST_CONCURRENCY = 3


@dataclass(frozen=True)
class SceneRead:
    resource: SceneResource
    page: int
    previous: dict[str, Any]
    operation: str


@dataclass(frozen=True)
class SceneReadResult:
    read: SceneRead
    observed_at: datetime
    result: ScenePage | Exception


@dataclass(frozen=True)
class SceneProgress:
    revision: int
    pages: int


def _complete(job: SceneJob, resource: SceneResource) -> bool:
    facts = job.facts.get(resource)
    if not isinstance(facts, dict) or not facts:
        return False
    if resource in {"identity", "minis"}:
        seen, total, pages = (
            facts.get(key) for key in ("seen", "total_number", "total_page")
        )
        if type(seen) is not int or type(total) is not int or type(pages) is not int:
            return False
        expected = (1 if total else 0) if resource == "identity" else (total + 49) // 50
        return (
            seen == total
            and total >= 0
            and pages >= 0
            and max(1, pages) == max(1, expected)
        )
    return True


def _pending(job: SceneJob) -> list[SceneResource]:
    # 五类物理读取都只依赖账户；目标未确认也并行准备公共资料，选择后直接复用。
    # Mini 目录仍优先启动，匹配只在本地进行，不能提前替用户决定目标。
    required = (
        RESOURCES
        if job.minis_id is not None
        else ("minis", "identity", "cta", "vbo", "regions")
    )
    return [resource for resource in required if not _complete(job, resource)]


def _pages(session: Session, job: SceneJob) -> dict[str, int]:
    return dict(
        session.exec(
            select(SceneJobPage.resource, func.max(col(SceneJobPage.page)))
            .where(
                SceneJobPage.tenant_id == job.tenant_id, SceneJobPage.job_id == job.id
            )
            .group_by(col(SceneJobPage.resource))
        ).all()
    )


def plan_scene_reads(session: Session, job: SceneJob, *, limit: int) -> list[SceneRead]:
    pages = _pages(session, job)
    result = []
    for resource in _pending(job)[:limit]:
        number = pages.get(resource, 0) + 1
        operation, _ = scene_arguments(
            resource=resource,
            advertiser_id=job.advertiser_id,
            bc_id=job.bc_id,
            page=number,
            minis_id=job.minis_id,
        )
        result.append(
            SceneRead(resource, number, dict(job.facts.get(resource, {})), operation)
        )
    return result


def advance_scene_cursor(session: Session, job: SceneJob) -> None:
    """顺序字段指向首个未完成资源；已成功的其它资源不会再次请求。"""
    pending = _pending(job)
    if pending:
        job.resource = pending[0]
        job.next_page = _pages(session, job).get(job.resource, 0) + 1
    else:
        job.resource, job.next_page = "done", 1


@contextmanager
def open_scene_reads(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    route: FrozenTikTokRoute,
    advertiser_id: str,
    minis_id: str | None,
    deadline: datetime,
    before_request: Callable[[], None],
    reads: list[SceneRead],
    can_start: Callable[[], bool],
) -> Iterator[Iterator[SceneReadResult]]:
    def fetch(read: SceneRead) -> SceneReadResult:
        observed = datetime.now(UTC)
        try:
            # 每个请求独立拥有 SDK 会话，仍逐次走共享配额和发送前权限围栏。
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=deadline,
                before_request=before_request,
            ) as gateway:
                result: ScenePage | Exception = gateway.scenes.read_page(
                    resource=read.resource,
                    advertiser_id=advertiser_id,
                    page=read.page,
                    minis_id=minis_id,
                )
        except Exception as error:
            result = error
        return SceneReadResult(read, observed, result)

    if not reads:
        yield iter(())
        return
    policies = [
        admission_policy(read.operation, channel=route.channel) for read in reads
    ]
    size = min(
        REQUEST_CONCURRENCY,
        len(reads),
        *(
            min(
                policy.app_max_inflight,
                policy.tenant_max_inflight,
                policy.advertiser_max_inflight,
            )
            for policy in policies
        ),
    )
    if size == 1:

        def serial() -> Iterator[SceneReadResult]:
            for read in reads:
                if not can_start():
                    return
                yield fetch(read)

        yield serial()
        return
    # 池只在 prefork 子进程本次任务中创建，异常退出也收齐已启动请求。
    with ThreadPoolExecutor(max_workers=size, thread_name_prefix="scene-read") as pool:

        def parallel() -> Iterator[SceneReadResult]:
            iterator = iter(reads)
            pending = set()
            for _ in range(size):
                read = next(iterator, None)
                if read is None or not can_start():
                    break
                pending.add(pool.submit(copy_context().run, fetch, read))
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    yield future.result()
                    read = next(iterator, None)
                    if read is not None and can_start():
                        pending.add(pool.submit(copy_context().run, fetch, read))

        yield parallel()
