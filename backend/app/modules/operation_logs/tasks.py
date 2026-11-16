"""操作日志独立小批规划：不等待报表筛查，沿现有control/后台额度执行。"""

import logging
from datetime import UTC, datetime, timedelta
from time import monotonic

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import or_
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db import engine
from app.jobs.celery_app import celery_app
from app.modules.business.models import BusinessScope
from app.modules.operation_logs.planning import plan_logs
from app.modules.reporting.planner import owned_account_timezones
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import AdSyncBc

logger = logging.getLogger(__name__)
PROGRESS = "operation_log_planning_v1"


def plan_next_batch(session: Session, *, now: datetime, batch_size: int = 8) -> int:
    # 独立服务年龄轮转BC；锁和游标在同一事务，小批提交后立即释放。
    plan = session.exec(
        select(AdSyncBc)
        .where(
            col(AdSyncBc.enabled).is_(True),
            or_(
                col(AdSyncBc.screening_progress)[PROGRESS]["next_scan_at"].astext.is_(
                    None
                ),
                col(AdSyncBc.screening_progress)[PROGRESS]["next_scan_at"].astext
                <= now.isoformat(),
            ),
        )
        .order_by(
            col(AdSyncBc.screening_progress)[PROGRESS]["last_turn"]
            .astext.asc()
            .nulls_first(),
            col(AdSyncBc.tenant_id),
            col(AdSyncBc.bc_id),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if plan is None:
        return 0
    state = plan.screening_progress.get(PROGRESS, {})
    zones = owned_account_timezones(session, plan)
    # 缺失时区不能猜UTC；等账户元数据修复后下一轮自然加入。
    accounts = sorted(a for a, zone in zones.items() if zone)
    after = state.get("after", "")
    remaining = [a for a in accounts if a > after]
    batch = (remaining or accounts)[:batch_size]
    sweep_complete = not batch or batch[-1] == accounts[-1]
    scope = session.get(BusinessScope, (plan.tenant_id, plan.bc_id))
    count = plan_logs(
        session,
        plan,
        now=now,
        accounts=batch,
        history_start=scope.start_date if scope is not None else None,
    )
    plan.screening_progress = {
        **plan.screening_progress,
        PROGRESS: {
            "after": "" if sweep_complete else batch[-1],
            # 小BC扫完后让出时间片，避免大BC每轮只能分到八分之一机会。
            "next_scan_at": (
                now + timedelta(minutes=5) if sweep_complete else now
            ).isoformat(),
            "last_turn": now.isoformat(),
            "accounts": len(accounts),
            "batch": len(batch),
        },
    }
    session.add(plan)
    return count


@celery_app.task(name="operation_logs.plan", soft_time_limit=20, time_limit=25)
def plan_operation_logs() -> int:
    if not settings.ADS_SYNC_ENABLED:
        return 0
    count, deadline = 0, monotonic() + 10
    # 每小批独立预算/提交，中断不会丢掉前面BC的进度；过期tick不堆消息。
    for _ in range(16):
        if monotonic() >= deadline:
            break
        with Session(engine) as session:
            try:
                with query_budget(session, seconds=3):
                    count_batch = plan_next_batch(session, now=datetime.now(UTC))
                session.commit()
                count += count_batch
            except SoftTimeLimitExceeded:
                session.invalidate()
                raise
            except (QueryBudgetExceeded, DBAPIError) as error:
                session.rollback()
                if (
                    isinstance(error, DBAPIError)
                    and getattr(error.orig, "sqlstate", None) != "57014"
                ):
                    raise
                logger.warning("operation_log_planning_budget_exhausted")
                break
    return count
