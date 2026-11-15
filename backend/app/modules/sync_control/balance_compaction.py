"""已存在的重复余额执行安全收敛；仅取消被更新执行完整承接的未在途批次。"""

from datetime import datetime
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import or_, update
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.modules.reporting.store_models import ReportBalanceRun
from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule, SyncScope
from app.modules.sync_control.requirements import execution_owner_current


def supersede_balance_run(session: Session, *, run_id: UUID, now: datetime) -> bool:
    """调用方每批独立短事务；事实、水位、费用和旧消息均不冒充已成功。"""
    old = session.exec(
        select(ReportBalanceRun)
        .where(
            col(ReportBalanceRun.id) == run_id,
            col(ReportBalanceRun.status) == "ACTIVE",
            or_(
                col(ReportBalanceRun.claimed_until).is_(None),
                col(ReportBalanceRun.claimed_until) <= now,
            ),
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if old is None or not old.remaining_advertiser_ids:
        return False
    replacement = session.exec(
        select(ReportBalanceRun)
        .where(
            col(ReportBalanceRun.tenant_id) == old.tenant_id,
            col(ReportBalanceRun.actor_id) == old.actor_id,
            col(ReportBalanceRun.frozen_route) == old.frozen_route,
            col(ReportBalanceRun.owner_epoch) == old.owner_epoch,
            col(ReportBalanceRun.status) == "ACTIVE",
            col(ReportBalanceRun.created_at) > old.created_at,
            col(ReportBalanceRun.remaining_advertiser_ids).contains(
                old.remaining_advertiser_ids
            ),
        )
        .order_by(col(ReportBalanceRun.created_at).desc(), col(ReportBalanceRun.id))
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).first()
    if replacement is None:
        return False
    # 先锁两个执行，再将全部scope按统一账户顺序一次持锁，不能先锁旧批次子集。
    session.exec(
        select(SyncScope)
        .where(
            col(SyncScope.id).in_(
                select(col(SyncExecutionScope.scope_id)).where(
                    col(SyncExecutionScope.balance_run_id).in_([old.id, replacement.id])
                )
            )
        )
        .order_by(
            col(SyncScope.tenant_id), col(SyncScope.advertiser_id), col(SyncScope.id)
        )
        .with_for_update(of=SyncScope)
        .execution_options(populate_existing=True)
    ).all()
    for run in (old, replacement):
        if (
            session.exec(
                select(col(ReportBalanceRun.id)).where(
                    col(ReportBalanceRun.id) == run.id,
                    execution_owner_current(ReportBalanceRun, "balance_run_id"),
                )
            ).first()
            is None
        ):
            return False
    children = session.exec(
        select(SyncExecutionScope)
        .where(col(SyncExecutionScope.balance_run_id).in_([old.id, replacement.id]))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    successors = {
        child.requirement_id: child
        for child in children
        if child.balance_run_id == replacement.id and child.status == "PENDING"
    }
    pending = [
        child
        for child in children
        if child.balance_run_id == old.id and child.status == "PENDING"
    ]
    if not pending:
        return False
    for child in pending:
        newer = successors.get(child.requirement_id)
        if (
            newer is None
            or newer.frozen_revision < child.frozen_revision
            or newer.frozen_requested_after < child.frozen_requested_after
            or child.execution_generation != old.claim_generation
            or newer.execution_generation != replacement.claim_generation
        ):
            return False
    for child in pending:
        child.status = "SUPERSEDED"
    scope_ids = sorted({child.scope_id for child in pending})
    # 后继已覆盖每个头。只释放旧执行自己的过期租约，不碰在途后继或其他执行。
    session.exec(
        select(SyncSchedule)
        .where(
            col(SyncSchedule.scope_id).in_(scope_ids),
            col(SyncSchedule.work_class) == "maintenance",
        )
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .with_for_update()
    ).all()
    typing_cast(SQLAlchemySession, session).execute(
        update(SyncSchedule)
        .where(
            col(SyncSchedule.scope_id).in_(scope_ids),
            col(SyncSchedule.work_class) == "maintenance",
            col(SyncSchedule.lease_execution_id) == old.id,
            col(SyncSchedule.lease_execution_generation) == old.claim_generation,
        )
        .values(
            lease_token=None,
            leased_until=None,
            lease_execution_id=None,
            lease_execution_generation=None,
        )
    )
    old.status, old.error_code, old.completed_at = (
        "CANCELLED",
        "balance_superseded",
        now,
    )
    old.claim_token = old.claimed_until = old.next_attempt_at = None
    session.flush()
    return True
