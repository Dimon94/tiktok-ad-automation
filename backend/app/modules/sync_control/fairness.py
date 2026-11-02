"""有工作类之间按请求成本平滑轮转；空闲类不保留闲置平台额度。"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col

from app.modules.reporting.store_models import (
    ReportDispatchCursor,
)

WEIGHTS = {"realtime": 50, "history": 30, "directory": 15, "maintenance": 5}


def choose_work_class(
    state: Mapping[str, float],
    available: Collection[str],
    *,
    cost: int = 1,
    retained: Collection[str] = (),
    accounting: Collection[str] | None = None,
) -> tuple[str | None, dict[str, float]]:
    eligible = sorted(set(available) & WEIGHTS.keys())
    if not eligible:
        return None, dict(state)
    result = {key: float(state.get(key, 0)) for key in WEIGHTS}
    winner = max(eligible, key=lambda key: (result[key] + WEIGHTS[key], WEIGHTS[key]))
    charged = set(eligible if accounting is None else accounting) & WEIGHTS.keys()
    charged.add(winner)
    total = sum(WEIGHTS[key] for key in charged)
    # 一次步骤可能发送50页；按同一物理单位推进冻结类份额才能维持请求成本比例。
    for key in charged:
        result[key] += WEIGHTS[key] * cost
    result[winner] -= total * cost
    # 有待运行或在途成本的类保留债务；真正空闲的类不累计虚拟份额。
    for key in WEIGHTS.keys() - set(eligible) - set(retained):
        result[key] = 0
    return winner, result


def due_tenants(
    session: Session, predicates: Sequence[ColumnElement[bool]], *, limit: int
) -> Sequence[UUID]:
    from sqlmodel import select

    from app.modules.reporting.store_models import ReportDispatchCursor
    from app.modules.sync_control.models import SyncSchedule
    from app.modules.tenants.models import Tenant

    # 租户数量远小于账户头数量；每租户找到一个到期头即停止，不能先扫完
    # 全部积压再GROUP BY。OFFSET 0保留相关探测，避免优化器重写成全量半连接。
    has_due = (
        select(col(SyncSchedule.scope_id))
        .where(col(SyncSchedule.tenant_id) == col(Tenant.id), *predicates)
        .correlate(Tenant)
        .limit(1)
        .offset(0)
        .exists()
    )
    return session.exec(
        select(col(Tenant.id))
        .outerjoin(
            ReportDispatchCursor,
            col(ReportDispatchCursor.tenant_id) == col(Tenant.id),
        )
        .where(has_due)
        .order_by(
            col(ReportDispatchCursor.last_dispatched_at).asc().nullsfirst(),
            col(Tenant.id),
        )
        .limit(limit)
    ).all()


def lock_tenant_cursor(
    session: Session, tenant_id: UUID
) -> ReportDispatchCursor | None:
    from datetime import UTC

    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert
    from sqlmodel import select

    # 先跳过被锁的现有行。无条件INSERT ON CONFLICT即使最终不插入，
    # 仍会等待另一事务更新的唯一索引项，使后面的SKIP LOCKED失去意义。
    cursor = session.get(
        ReportDispatchCursor,
        tenant_id,
        with_for_update={"skip_locked": True},
        populate_existing=True,
    )
    if cursor is None:
        existing = session.exec(
            select(col(ReportDispatchCursor.tenant_id)).where(
                col(ReportDispatchCursor.tenant_id) == tenant_id
            )
        ).first()
        if existing is not None:
            return None  # MVCC只读确认已存在，不能为锁竞争再次尝试初始化。
        # 只在首次创建时竞争此非阻塞锁；并发初始化也不能在唯一键上等待。
        if (
            not session.connection()
            .execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"tkada:sync-cursor-init:{tenant_id}"},
            )
            .scalar_one()
        ):
            return None
        session.connection().execute(
            insert(ReportDispatchCursor)
            .values(
                tenant_id=tenant_id,
                last_dispatched_at=datetime(1970, 1, 1, tzinfo=UTC),
                fairness_state={},
            )
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        cursor = session.get(
            ReportDispatchCursor,
            tenant_id,
            with_for_update={"skip_locked": True},
            populate_existing=True,
        )
    if cursor is None:
        return None
    from app.modules.sync_control.costs import settle_costs

    settle_costs(session, cursor)
    return cursor
