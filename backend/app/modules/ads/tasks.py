"""Celery 入口：每次推进一个广告目录页，目录完成后才发布。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from redis import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import DirectoryQuery
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.modules.accounts.routing import verify_route
from app.modules.ads.directory_calibration import PERIODIC_KEY
from app.modules.ads.directory_dispatch import (
    PUBLISH_TASK_NAME,
    TARGETED_TASK_NAME,
    TASK_NAME,
    enqueue_directory_step,
    is_live_directory,
)
from app.modules.ads.directory_services import SERVICE_KEY
from app.modules.ads.material_directory_children import (
    MATERIAL_CHILD_KEY,
    material_child_scope,
)
from app.modules.ads.sync import (
    directory_run_superseded,
    publish_directory,
    stage_directory_page,
)
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun

# 最大重试次数限制，防止死循环（数据库错误导致的重试）。
MAX_CLAIM_GENERATION = 100
_LEASE_SECONDS = 90
_PUBLISH_LEASE_SECONDS = 210
_LOCK_TIMEOUT = "20s"
_TRANSACTION_TIMEOUT = "45s"
_PUBLISH_TRANSACTION_TIMEOUT = "180s"
logger = logging.getLogger(__name__)


@contextmanager
def _directory_session(
    database_engine: Any, *, publish_only: bool = False
) -> Iterator[Session]:
    """领取和发布均使用有界事务，异常时丢弃物理连接以释放服务器行锁。"""
    with Session(database_engine) as session:
        try:
            session.connection().execute(
                text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
            )
            session.connection().execute(text("SET LOCAL statement_timeout = '30s'"))
            # 仅单槽本地发布给大目录累计IO独立预算；读取/实时定向仍是45秒。
            # 180秒从领取前开始，早于195秒软限及210秒发布租约；不延长单语句或等锁。
            timeout = (
                _PUBLISH_TRANSACTION_TIMEOUT if publish_only else _TRANSACTION_TIMEOUT
            )
            session.connection().execute(
                text(f"SET LOCAL transaction_timeout = '{timeout}'")
            )
            # 发布没有外部请求；即使进程被软超时中断且回滚失败，也由服务器兜底。
            session.connection().execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '10s'")
            )
            yield session
        except BaseException:
            # psycopg 被 Celery 信号打断后可能仍有未完成命令，普通 close/rollback
            # 会报 another command is already in progress，必须直接关闭物理连接。
            session.invalidate()
            raise


def _context(tenant_id: str, actor_id: str) -> TenantContext:
    try:
        return TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "目录任务操作者无效") from exc


def _payload(payload: dict[str, Any]) -> tuple[UUID, int]:
    validate_dispatch_payload(payload)
    if set(payload) != {"run_id", "claim_generation"}:
        raise DomainError("dispatch_payload_invalid", "目录任务参数无效")
    try:
        run_id = UUID(str(payload["run_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "目录运行标识无效") from exc
    generation = payload["claim_generation"]
    if type(generation) is not int or generation < 1:
        raise DomainError("dispatch_payload_invalid", "目录领取代数无效")
    return run_id, generation


@dataclass
class _CommittedDirectoryWork:
    """只记录已提交的本地工作；失败/重复消息不能触发额外派发。"""

    refill: bool = False
    deliver: bool = False


def _deliver_committed_work(database_engine, work: _CommittedDirectoryWork) -> None:
    if not settings.ADS_SYNC_ENABLED:
        return
    deliver = work.deliver
    if work.refill:
        try:
            from app.modules.reporting.job_tasks import _dispatch_directories

            # 仅在释放目录槽后补位；分页续步复用原槽，不能每页扫描积压。
            # 同时考虑两池：全量主目录可能生成实时素材子运行。复用原窗口锁、
            # 租户轮转和背景保底；慢查询回滚本次领取，Beat 后续仍可恢复。
            with Session(database_engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='2s'"))
                session.connection().execute(text("SET LOCAL lock_timeout='100ms'"))
                session.connection().execute(text("SET LOCAL transaction_timeout='3s'"))
                claimed = _dispatch_directories(session, datetime.now(UTC))
            deliver = deliver or claimed > 0
        except Exception:
            logger.exception("directory direct refill deferred")
    if deliver:
        try:
            from app.jobs.tasks import drain_dispatch

            # 业务和领取事务均已提交，再公平、有界发送原 outbox。发送失败保留
            # 同一消息身份及原退避，不得把已发布目录改成失败。
            drain_dispatch()
        except Exception:
            logger.exception("directory continuation delivery deferred")


def _finish_superseded_directory(
    session: Session, run: AdDirectoryRun, work: _CommittedDirectoryWork
) -> str:
    # 这是不可再发布的明确终态，不删除已暂存页或把旧数据伪装成成功发布。
    run.status, run.error_code = "STALE", "directory_run_stale"
    run.completed_at = datetime.now(UTC)
    run.claim_token = run.claimed_until = None
    session.commit()
    work.refill = True
    return "READY"


def run_directory_step(
    *,
    database_engine: Any,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    claim_generation: int,
    publish_only: bool = False,
) -> str:
    work = _CommittedDirectoryWork()
    try:
        return _execute_directory_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            run_id=run_id,
            claim_generation=claim_generation,
            publish_only=publish_only,
            work=work,
        )
    finally:
        _deliver_committed_work(database_engine, work)


def _execute_directory_step(
    *,
    database_engine: Any,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    claim_generation: int,
    publish_only: bool,
    work: _CommittedDirectoryWork,
) -> str:
    try:
        return _run_directory_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            run_id=run_id,
            claim_generation=claim_generation,
            publish_only=publish_only,
            work=work,
        )
    except (DBAPIError, SoftTimeLimitExceeded) as error:
        retryable = isinstance(error, SoftTimeLimitExceeded) or (
            isinstance(error, DBAPIError)
            and (
                error.connection_invalidated
                or getattr(error.orig, "sqlstate", None)
                in {"25P04", "57014", "55P03", "40001", "40P01", "57P01", "08006"}
            )
        )
        if not publish_only or not retryable:
            raise
        # 本地发布已整体回滚/断连；保存下一代重试，不能让已消费的outbox空占6小时。
        # 这里不重放任何外部请求，末页及冻结范围仍由正常发布流程重新核验。
        with _directory_session(database_engine) as session:
            run = session.exec(
                select(AdDirectoryRun)
                .where(
                    AdDirectoryRun.id == run_id,
                    AdDirectoryRun.tenant_id == context.tenant_id,
                )
                .with_for_update(skip_locked=True)
            ).one_or_none()
            now = datetime.now(UTC)
            if (
                run is None
                or run.actor_id != context.actor_id
                or run.claim_generation != claim_generation
                or run.published_version is not None
                or run.status in {"COMPLETE", "FAILED", "CANCELLED", "STALE"}
                or run.claimed_until is not None
                and run.claimed_until > now
            ):
                return "FAILED"

            # 检查重试次数上限，防止死循环
            if run.claim_generation >= MAX_CLAIM_GENERATION:
                run.status, run.error_code = "FAILED", "max_retries_exceeded"
                run.completed_at = now
                run.claim_token = run.claimed_until = None
                session.commit()
                work.refill = True
                logger.error(
                    "directory publish failed: run=%s generation=%s reason=max_retries_exceeded",
                    run_id,
                    claim_generation,
                )
                return "FAILED"

            run.claim_generation += 1
            run.claim_token = run.claimed_until = None
            run.status, run.error_code = "WAITING_REMOTE", "directory_publish_retry"
            run.next_attempt_at = now + timedelta(minutes=2)
            # 退避期间不预占目录窗口；scanner到期只发本地发布，其他账户可先执行。
            session.commit()
            work.refill = True
        logger.warning(
            "directory publish deferred: run=%s generation=%s error=%s "
            "sqlstate=%s statement_sha256=%s",
            run_id,
            claim_generation,
            type(error).__name__,
            getattr(error.orig, "sqlstate", None)
            if isinstance(error, DBAPIError)
            else None,
            # 只记录语句形状的摘要，不展开SQL、绑定参数或数据库错误正文。
            hashlib.sha256((error.statement or "").encode()).hexdigest()
            if isinstance(error, DBAPIError)
            else None,
        )
        return "WAIT"


def _run_directory_step(
    *,
    database_engine: Any,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    claim_generation: int,
    publish_only: bool = False,
    work: _CommittedDirectoryWork,
) -> str:
    # 实例总开关同时拦截已经排队的目录任务；重新开启后由扫描器恢复。
    if not settings.ADS_SYNC_ENABLED:
        return "WAIT"
    with _directory_session(database_engine, publish_only=publish_only) as session:
        run = session.exec(
            select(AdDirectoryRun)
            .where(
                AdDirectoryRun.id == run_id,
                AdDirectoryRun.tenant_id == context.tenant_id,
            )
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if run is None:
            raise DomainError("directory_run_not_found", "目录同步运行不存在")
        if run.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "目录任务操作者不匹配")
        if run.claim_generation != claim_generation:
            return "FAILED"
        if (
            run.status in {"COMPLETE", "FAILED", "CANCELLED", "STALE"}
            or run.published_version is not None
        ):
            return "FAILED" if run.status == "FAILED" else "READY"
        now = datetime.now(UTC)
        # A crashed worker leaves a short lease.  A live duplicate must not
        # open a second provider call; the scanner advances generation only
        # after this lease expires.
        if run.claimed_until is not None and run.claimed_until > now:
            return "WAIT"
        run.claim_token = uuid4()
        claim_token = run.claim_token
        run.claimed_until = now + timedelta(
            seconds=_PUBLISH_LEASE_SECONDS if publish_only else _LEASE_SECONDS
        )
        route = _route(run.frozen_route)
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=run.advertiser_id,
            capability="read",
        )
        if directory_run_superseded(session, run):
            return _finish_superseded_directory(session, run, work)
        terminal = session.exec(
            select(AdDirectoryPage.page)
            .where(
                AdDirectoryPage.run_id == run_id,
                col(AdDirectoryPage.complete).is_(True),
            )
            .limit(1)
        ).first()
        if terminal is not None:
            # 末页已持久化：独占一次任务期限做本地发布，恢复时也不再读远端。
            try:
                publish_directory(
                    session, run_id=run_id, claim_generation=claim_generation
                )
            except DomainError as error:
                if error.code != "directory_run_stale":
                    raise
                # 等账户发布锁期间也可能被新运行取代，独立保存终态并补位。
                return _finish_superseded_directory(session, run, work)
            run.claim_token = run.claimed_until = None
            session.commit()
            work.refill = True
            return "READY"
        if publish_only:
            raise DomainError("directory_incomplete", "目录发布缺少已暂存末页")
        material_scope = material_child_scope(session, run)
        query_data = dict(run.query)
        query_data.pop(MATERIAL_CHILD_KEY, None)
        query_data.pop(PERIODIC_KEY, None)
        query_data.pop(SERVICE_KEY, None)
        # Celery retries must resume the persisted cursor.  The durable query is
        # the contract; page is the only mutable transport cursor.
        query_data["page"] = run.next_page
        query_data["ids"] = tuple(query_data.get("ids", ()))
        query_data["parent_ids"] = tuple(query_data.get("parent_ids", ()))
        query = DirectoryQuery(**query_data)
        primary_only = (
            run.channel == "OFFICIAL_API"
            and run.kind == "ad"
            and run.ad_type == "SMART_PLUS"
        )
        workload = "background" if run.workload == "background" else "live"
        session.commit()

    deadline = datetime.now(UTC) + timedelta(seconds=45)
    try:
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            workload=workload,
        ) as gateway:
            if material_scope is not None:
                page = gateway.ads.read_material_page(
                    query, ads=material_scope[0], materials=material_scope[1]
                )
            elif primary_only:
                page = gateway.ads.read_primary_page(query)
            else:
                page = gateway.ads.read_page(query)
    except AccountAdmissionDeferred as error:
        with _directory_session(database_engine) as session:
            current = session.get(AdDirectoryRun, run_id, with_for_update=True)
            if (
                current is None
                or current.claim_generation != claim_generation
                or current.claim_token != claim_token
            ):
                return "WAIT"
            # 本地准入未获额度不是读取失败，不能留下 90 秒死租约；换代隔离旧消息，
            # 保留已暂存分页，按返回的等待时间持久安排同页续步，不扩大窗口或平台配额。
            current.status = "WAITING_REMOTE"
            current.error_code = error.code
            current.next_attempt_at = datetime.now(UTC) + timedelta(
                milliseconds=max(1000, error.retry_after_ms)
            )
            current.claim_generation += 1
            current.claim_token = current.claimed_until = None
            enqueue_directory_step(
                session, current, available_at=current.next_attempt_at
            )
            session.commit()
            work.deliver = True
        return "WAIT"
    with _directory_session(database_engine) as session:
        stage_directory_page(
            session,
            run_id=run_id,
            page=page,
            claim_generation=claim_generation,
        )
        current = session.get(AdDirectoryRun, run_id)
        if current is None or (not page.complete and page.next_page is None):
            raise DomainError("directory_incomplete", "目录页缺少后续游标")
        if current.claim_generation != claim_generation:
            # 网络请求期间可能已经换代；相同旧页可复用，但不能清掉新 worker 的租约。
            return "FAILED"
        # 末页之后的游标只标识发布续步；领取时先识别暂存末页，不会请求该页。
        next_page = page.page + 1 if page.complete else page.next_page
        assert next_page is not None  # 上方已经拒绝非末页缺失游标。
        current.next_page = next_page
        current.status = "RUNNING"
        current.error_code = None
        current.next_attempt_at = datetime.now(UTC)
        current.claim_token = current.claimed_until = None
        # 续步与页同事务提交，统一扫描器的幂等键，消息丢失仍可换代恢复。
        enqueue_directory_step(session, current, publish_only=page.complete)
        session.commit()
        work.deliver = True
        return "CONTINUE"


@celery_app.task(  # type: ignore[untyped-decorator]
    name=TARGETED_TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def targeted_step(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    run_id, generation = _payload(payload)
    context = _context(tenant_id, actor_id)
    with Session(engine) as session:
        run = session.get(AdDirectoryRun, run_id)
        # 独立入口只接收有界 ID 查询；不能借回读通道搬运全量目录。
        if (
            run is None
            or run.tenant_id != context.tenant_id
            or not is_live_directory(run)
        ):
            raise DomainError("directory_request_invalid", "定向回读缺少对象范围")
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        return run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            run_id=run_id,
            claim_generation=generation,
        )


def _route(value: dict[str, Any]):
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

    try:
        return FrozenTikTokRoute.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("frozen_route_changed", "目录任务冻结路由无效") from exc


@celery_app.task(  # type: ignore[untyped-decorator]
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def sync_step(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    run_id, generation = _payload(payload)
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        return run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=_context(tenant_id, actor_id),
            run_id=run_id,
            claim_generation=generation,
        )


@celery_app.task(  # type: ignore[untyped-decorator]
    name=PUBLISH_TASK_NAME,
    time_limit=210,
    soft_time_limit=195,
    acks_late=True,
    reject_on_worker_lost=True,
)
def publish_step(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    run_id, generation = _payload(payload)
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        return run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=_context(tenant_id, actor_id),
            run_id=run_id,
            claim_generation=generation,
            publish_only=True,
        )


__all__ = [
    "TASK_NAME",
    "PUBLISH_TASK_NAME",
    "sync_step",
    "publish_step",
    "run_directory_step",
]
