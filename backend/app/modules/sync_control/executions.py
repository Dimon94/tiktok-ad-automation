"""外部读取前释放业务事务；回包只能由仍持有原围栏的执行接受。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS
from app.modules.accounts.routing import verify_route
from app.modules.reporting.store_models import (
    ReportSyncJob,
)
from app.modules.sync_control.models import SyncExecutionScope, SyncScope


class ExecutionLost(Exception):
    """失租不是新执行失败；旧执行应静默退出，不能修改新代状态。"""


def fetch_page(method: Callable[..., Any], arguments: Mapping[str, Any]) -> Any:
    """网络阶段不接收 Session，也没有事务或对象锁的所有权。"""
    return method(**arguments)


class TransactionBoundaryReader:
    """复用各业务 reader 和校验器，在每个远端读取边界提交完整私有证据。

    财务事实仍只由完整快照后的 publisher 提交；页间可提交的是已校验私有页，
    或已独立证明的素材身份链接。网络返回前重新锁定并核验冻结授权和执行代数。
    """

    def __init__(self, session: Session, job: ReportSyncJob, reader: Any) -> None:
        self.session, self.job, self.reader = session, job, reader
        self.job_id, self.claim_token = job.id, job.claim_token
        self.generation = getattr(job, "claim_generation", 0)
        self.owner_epoch = getattr(job, "owner_epoch", 1)
        self.route = FrozenTikTokRoute.model_validate(job.frozen_route)
        self.context = TenantContext(
            tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
        )
        self.advertisers = tuple(job.advertiser_ids)

    def _check(self) -> ReportSyncJob:
        scopes = self.session.exec(
            select(SyncExecutionScope, SyncScope)
            .join(SyncScope, col(SyncScope.id) == col(SyncExecutionScope.scope_id))
            .where(col(SyncExecutionScope.report_job_id) == self.job_id)
            .order_by(col(SyncScope.id), col(SyncExecutionScope.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        current = (
            typing_cast(SQLAlchemySession, self.session)
            .execute(
                sa_select(
                    col(ReportSyncJob.status),
                    col(ReportSyncJob.claim_token),
                    col(ReportSyncJob.claim_generation),
                    col(ReportSyncJob.owner_epoch),
                    col(ReportSyncJob.claimed_until),
                )
                .where(col(ReportSyncJob.id) == self.job_id)
                .with_for_update()
            )
            .one_or_none()
        )
        if (
            current is None
            or current.status != "RUNNING"
            or current.claim_token != self.claim_token
            or getattr(current, "claim_generation", 0) != self.generation
            or getattr(current, "owner_epoch", 1) != self.owner_epoch
            or current.claimed_until is None
            or current.claimed_until <= datetime.now(UTC)
        ):
            raise ExecutionLost()
        for advertiser in self.advertisers:
            verify_route(
                self.session,
                context=self.context,
                route=self.route,
                advertiser_id=advertiser,
                capability="read",
            )
        for child, scope in scopes:
            if (
                child.owner_epoch != scope.owner_epoch
                or scope.owner_epoch != self.owner_epoch
            ):
                raise ExecutionLost()
            if child.observed_at is None:
                stamp = self.job.progress.get("snapshot_observed_at")
                child.observed_at = (
                    datetime.fromisoformat(stamp) if stamp else datetime.now(UTC)
                )
            child.execution_generation = self.generation
        return self.job

    def external(self, call: Callable[[], Any]) -> Any:
        """私有归档也不跨网络持有DB锁；回读后重验原执行围栏。"""
        self._check()
        self.session.commit()
        result = call()
        self._check()
        return result

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.reader, name)
        if not callable(method):
            return method

        def read(**arguments: Any) -> Any:
            # 关联扫描持有progress字典引用；围栏只回读标量，不能用ORM刷新把
            # 正在写入的字典换掉。HTTP前显式保存已完成的游标/查询范围。
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(self.job, "progress")
            self.session.flush()
            self._check()
            self.session.commit()
            try:
                result = fetch_page(method, arguments)
            except SDK_SCOPE_INTERRUPTS:
                # 中断可能仍有已发送请求；不能让失租检查覆盖中断并把unknown成本闭合。
                # 原样退出，不接受返回、不改业务状态，由外层保留未知发送收据。
                raise
            except BaseException:
                # 错误同样必须先核验围栏；旧worker不能把新worker改成WAITING/FAILED。
                self._check()
                raise
            self._check()
            return result

        return read
