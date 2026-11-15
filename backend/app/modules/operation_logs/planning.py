"""昨日增量与历史缺口独立推进，复用账户归属和持久需求公平调度。"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import cast as typing_cast
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.modules.operation_logs.models import OperationCoverage, OperationSyncCursor
from app.modules.reporting.jobs import enqueue_jobs
from app.modules.reporting.planner import (
    _common,
    _timezones,
    local_today,
    owned_accounts,
)
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncScope,
)
from app.modules.sync_control.requirements import execution_owner_current
from app.modules.sync_control.scopes import scope_key


def plan_logs(
    session: Session,
    plan: AdSyncBc,
    *,
    now: datetime,
    accounts: list[str] | None = None,
    history_start: date | None = None,
) -> int:
    owned = set(owned_accounts(session, plan))
    candidates = (
        sorted(owned.intersection(accounts)) if accounts is not None else sorted(owned)
    )
    if not candidates:
        return 0
    zones = _timezones(session, plan.tenant_id, candidates)
    typing_cast(SQLAlchemySession, session).execute(
        insert(OperationSyncCursor)
        .values(
            [
                OperationSyncCursor(
                    tenant_id=plan.tenant_id, advertiser_id=a
                ).model_dump()
                for a in candidates
            ]
        )
        .on_conflict_do_nothing()
    )
    cursors = session.exec(
        select(OperationSyncCursor)
        .where(
            col(OperationSyncCursor.tenant_id) == plan.tenant_id,
            col(OperationSyncCursor.advertiser_id).in_(candidates),
        )
        .order_by(col(OperationSyncCursor.advertiser_id))
        .with_for_update()
    ).all()
    windows: dict[tuple[str, date, date], list[list[str]]] = defaultdict(list)
    for cursor in cursors:
        advertiser = cursor.advertiser_id
        zone = zones[advertiser]
        today = local_today(zone, now)
        zone = typing_cast(str, zone)
        end = today - timedelta(days=1)
        first = min(history_start or end, end)
        known = set(
            session.exec(
                select(col(OperationCoverage.stat_date)).where(
                    col(OperationCoverage.tenant_id) == plan.tenant_id,
                    col(OperationCoverage.advertiser_id) == advertiser,
                    col(OperationCoverage.stat_date) >= first,
                    col(OperationCoverage.stat_date) <= end,
                )
            ).all()
        )
        # 只检查当前冻结授权与时区的需求；其他BC/旧授权不能替代本轮执行。
        key = scope_key(
            plan.frozen_route,
            plan.actor_id,
            advertiser,
            "operation_log",
            {"campaign_ids": []},
            "report-v2",
            timezone=zone,
        )
        active = (
            select(col(SyncExecutionScope.id))
            .join(
                ReportSyncJob,
                col(ReportSyncJob.id) == col(SyncExecutionScope.report_job_id),
            )
            .where(
                col(SyncExecutionScope.requirement_id) == col(SyncRequirement.id),
                col(SyncExecutionScope.owner_epoch) == col(SyncScope.owner_epoch),
                col(SyncExecutionScope.status) == "PENDING",
                col(ReportSyncJob.status).in_(["QUEUED", "WAITING", "RUNNING"]),
                execution_owner_current(ReportSyncJob, "report_job_id"),
            )
            .correlate(SyncRequirement, SyncScope)
            .exists()
        )
        for requirement in session.exec(
            select(SyncRequirement)
            .join(SyncScope, col(SyncScope.id) == col(SyncRequirement.scope_id))
            .where(
                col(SyncScope.identity_digest) == key.digest,
                col(SyncRequirement.bucket_key) >= first.isoformat(),
                col(SyncRequirement.bucket_key) <= end.isoformat(),
                or_(
                    active,
                    col(SyncRequirement.requested_after)
                    >= datetime.combine(today, time.min, ZoneInfo(zone)),
                ),
            )
        ).all():
            # 已承接的缺口不升级revision；终态失败最多次日重新规划一次。
            known.add(date.fromisoformat(requirement.bucket_key))
        if end not in known:
            windows["daily", end, end].append([advertiser])
        cursor.recent_day = today
        if history_start is not None:
            last = end - timedelta(days=1)
            while last >= history_start and last in known:
                last -= timedelta(days=1)
            if last >= history_start:
                start = last
                # 从最近缺口向前取连续片，不跨成功/在途日期，每片最多29个完整日。
                while (
                    start > history_start
                    and (last - start).days < 28
                    and start - timedelta(days=1) not in known
                ):
                    start -= timedelta(days=1)
                windows["history", start, last].append([advertiser])
                cursor.history_cursor = start - timedelta(days=1)
            else:
                cursor.history_cursor = history_start - timedelta(days=1)
            cursor.history_sweep_day = today
        session.add(cursor)
    created = 0
    for (tier, start, end), groups in windows.items():
        created += len(
            enqueue_jobs(
                session,
                **_common(plan),
                kind="operation_log",
                tier=tier,
                advertiser_groups=groups,
                start=start,
                end=end,
                requested_at=now,
                execution_window_days=29,
            )
        )
    return created
