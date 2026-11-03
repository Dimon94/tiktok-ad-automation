"""有工作类之间按请求成本平滑轮转；空闲类不保留闲置平台额度。"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
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
