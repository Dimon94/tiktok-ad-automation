"""原planner统一进入需求归并；账户打包和tier变化不再复制业务需求。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from typing import cast as typing_cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.store_models import (
    ReportSyncJob,
    ReportSyncPage,
)
from app.modules.sync_control.contracts import BucketKey
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
)
from app.modules.sync_control.requirements import (
    execution_owner_current,
    freeze_execution_scope,
    merge_requirement,
    pending_work_class,
)
from app.modules.sync_control.schedule import report_class, report_endpoint, wake_scope
from app.modules.sync_control.scopes import ensure_scope


def _strictly_unstarted() -> tuple[ColumnElement[bool], ...]:
    # 代数/租约之外也核验私有页和观察证据，不能把迁移来的半成品当作未启动。
    return (
        col(ReportSyncJob.status) == "QUEUED",
        col(ReportSyncJob.claim_generation) == 0,
        col(ReportSyncJob.claim_token).is_(None),
        col(ReportSyncJob.claimed_until).is_(None),
        col(ReportSyncJob.progress)
        .op("-")("requested_at")
        .op("-")("execution_window_days")
        == {},
        ~select(col(ReportSyncPage.job_id))
        .where(col(ReportSyncPage.job_id) == col(ReportSyncJob.id))
        .exists(),
        ~select(col(SyncExecutionScope.id))
        .where(
            col(SyncExecutionScope.report_job_id) == col(ReportSyncJob.id),
            col(SyncExecutionScope.observed_at).is_not(None),
        )
        .exists(),
    )


def _adjacent_unstarted(
    session: Session,
    *,
    scope_ids: Sequence[UUID],
    advertisers: list[str],
    kind: str,
    start: date,
    end: date,
    cutoff: date,
) -> ReportSyncJob | None:

    # 当天及实时金额保持短查询；小时报表合同仍为单日，不扩成跨日。
    if end >= cutoff or kind == "campaign_hour":
        return None
    eligible = [
        *_strictly_unstarted(),
        # 已由首个真实响应证明需要拆分的范围不可被后来需求重新扩回。
        ~col(ReportSyncJob.progress).op("?")("execution_window_days"),
        col(ReportSyncJob.kind) == kind,
        col(ReportSyncJob.advertiser_ids) == advertisers,
        col(ReportSyncJob.start_date) <= end + timedelta(days=1),
        col(ReportSyncJob.end_date) >= start - timedelta(days=1),
        col(ReportSyncJob.end_date) < cutoff,
        func.greatest(col(ReportSyncJob.end_date), end)
        - func.least(col(ReportSyncJob.start_date), start)
        < 30,
        execution_owner_current(ReportSyncJob, "report_job_id"),
        # BASIC批次的全部账户必须仍是同一冻结查询；仅一个scope相交不足以合并。
        ~select(col(SyncExecutionScope.id))
        .where(
            col(SyncExecutionScope.report_job_id) == col(ReportSyncJob.id),
            col(SyncExecutionScope.scope_id).not_in(scope_ids),
        )
        .exists(),
        col(ReportSyncJob.id).in_(
            select(col(SyncExecutionScope.report_job_id)).where(
                col(SyncExecutionScope.scope_id).in_(scope_ids),
                col(SyncExecutionScope.status) == "PENDING",
            )
        ),
    ]
    eligible.append(
        or_(
            col(ReportSyncJob.priority) > 2,
            col(ReportSyncJob.end_date) < cutoff - timedelta(days=1),
        )
    )
    return session.exec(
        select(ReportSyncJob)
        .where(*eligible)
        .order_by(col(ReportSyncJob.created_at), col(ReportSyncJob.id))
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).first()


def _missing_windows(
    missing: dict[str, list[SyncRequirement]],
    advertisers: Sequence[str],
    *,
    max_days: int,
) -> list[tuple[date, date, list[str]]]:
    # 只查询尚无执行承接的日期。重叠请求先减去已有冻结子桶，再按连续日期打包。
    # 不同账户只有缺口范围完全相同才共享一次请求，不能用大包范围重读已覆盖日期。
    windows: dict[tuple[date, date], list[str]] = {}
    for advertiser in advertisers:
        dates = sorted(
            date.fromisoformat(row.bucket_key) for row in missing[advertiser]
        )
        start = end = dates[0]
        for day in dates[1:]:
            if day == end + timedelta(days=1) and (day - start).days < max_days:
                end = day
            else:
                windows.setdefault((start, end), []).append(advertiser)
                start = end = day
        windows.setdefault((start, end), []).append(advertiser)
    return [
        (start, end, accounts) for (start, end), accounts in sorted(windows.items())
    ]


def enqueue_report_requirements(
    session: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    kind: str,
    priority: int,
    advertiser_groups: Iterable[Sequence[str]],
    start: date,
    end: date,
    frozen_route: dict[str, Any],
    actor_id: UUID,
    campaign_ids: Sequence[str] = (),
    due_at: datetime | None = None,
    requested_at: datetime | None = None,
    execution_window_days: int | None = None,
) -> list[UUID]:
    """按业务批批量归并；SQL往返数随批次增长，不随账户×日期逐行增长。"""
    from sqlalchemy import case
    from sqlalchemy.dialects.postgresql import insert

    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.sync_control.models import SyncScope
    from app.modules.sync_control.scopes import scope_key

    if kind in {"material_day", "material_links"}:
        raise DomainError("report_kind_retired", "旧素材报表及补链任务已退休")
    if execution_window_days is not None and not 1 <= execution_window_days <= 30:
        raise ValueError("execution_window_days must be within 1..30")
    now = datetime.now(UTC)
    requested, due = requested_at or now, due_at or now
    route = FrozenTikTokRoute.model_validate(frozen_route)
    if (
        route.tenant_id != tenant_id
        or route.bc_id != bc_id
        or requested.utcoffset() is None
    ):
        raise ValueError("report requirement must use its tenant's frozen route")
    groups = [sorted(set(group)) for group in advertiser_groups if group]
    advertisers = sorted({account for group in groups for account in group})
    if not advertisers:
        return []
    accounts = {
        row.advertiser_id: row
        for row in session.exec(
            select(AdvertiserAccount).where(
                col(AdvertiserAccount.tenant_id) == tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(advertisers),
            )
        ).all()
    }
    query = {"campaign_ids": sorted(campaign_ids)}
    scope_values, classes = [], {}
    for advertiser in advertisers:
        account = accounts.get(advertiser)
        if account is None or not account.timezone:
            raise ValueError("report requirement requires known account timezone")
        key = scope_key(
            route,
            actor_id,
            advertiser,
            kind,
            query,
            "report-v2",
            timezone=account.timezone,
        )
        scope_values.append(
            SyncScope(
                tenant_id=tenant_id,
                actor_id=actor_id,
                advertiser_id=advertiser,
                bc_id=bc_id,
                connection_id=route.connection_id,
                channel=route.channel,
                frozen_route=route.model_dump(mode="json"),
                resource=kind,
                timezone=account.timezone,
                identity_digest=key.digest,
                query_contract={"query": query, "metrics": "report-v2"},
            ).model_dump()
        )
        local_today = now.astimezone(ZoneInfo(account.timezone)).date()
        classes[advertiser] = (
            "realtime"
            if priority <= 2 and start == end and end >= local_today - timedelta(days=1)
            else "history"
        )
    session.connection().execute(
        insert(SyncScope).on_conflict_do_nothing(index_elements=["identity_digest"]),
        scope_values,
    )
    scopes = {
        row.advertiser_id: row
        for row in session.exec(
            select(SyncScope)
            .where(
                col(SyncScope.identity_digest).in_(
                    [row["identity_digest"] for row in scope_values]
                )
            )
            .order_by(col(SyncScope.id))
            .with_for_update()
        ).all()
    }
    values = []
    for advertiser in advertisers:
        scope = scopes[advertiser]
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            values.append(
                SyncRequirement(
                    tenant_id=tenant_id,
                    scope_id=scope.id,
                    bucket_key=day.isoformat(),
                    bucket={"local_date": day.isoformat()},
                    requested_after=requested,
                    oldest_pending_at=requested,
                    source=classes[advertiser],
                ).model_dump()
            )
    requirements: list[SyncRequirement] = []
    for offset in range(0, len(values), 1000):
        statement = insert(SyncRequirement).values(values[offset : offset + 1000])
        newer = statement.excluded.requested_after > col(
            SyncRequirement.requested_after
        )
        returning = statement.on_conflict_do_update(
            index_elements=["scope_id", "bucket_key"],
            set_={
                "desired_revision": col(SyncRequirement.desired_revision)
                + case((newer, 1), else_=0),
                "requested_after": func.greatest(
                    col(SyncRequirement.requested_after),
                    statement.excluded.requested_after,
                ),
                "oldest_pending_at": case(
                    (
                        (
                            col(SyncRequirement.satisfied_revision)
                            == col(SyncRequirement.desired_revision)
                        )
                        & newer,
                        requested,
                    ),
                    else_=col(SyncRequirement.oldest_pending_at),
                ),
                "source": pending_work_class(statement.excluded.source),
            },
        ).returning(SyncRequirement)
        requirements.extend(
            typing_cast(SQLAlchemySession, session)
            .execute(
                select(SyncRequirement)
                .from_statement(returning)
                .execution_options(populate_existing=True)
            )
            .scalars()
        )
    pending = [
        row for row in requirements if row.satisfied_revision < row.desired_revision
    ]
    due_by_requirement: dict[UUID, datetime] = {}
    existing: dict[UUID, tuple[ReportSyncJob, SyncExecutionScope]] = {}
    if pending:
        for job, child in session.exec(
            select(ReportSyncJob, SyncExecutionScope)
            .join(
                SyncExecutionScope,
                col(SyncExecutionScope.report_job_id) == col(ReportSyncJob.id),
            )
            .join(SyncScope, col(SyncScope.id) == col(SyncExecutionScope.scope_id))
            .where(
                col(SyncExecutionScope.requirement_id).in_([row.id for row in pending]),
                col(SyncExecutionScope.owner_epoch) == col(SyncScope.owner_epoch),
                col(ReportSyncJob.owner_epoch) == col(SyncScope.owner_epoch),
                col(ReportSyncJob.status).in_(["QUEUED", "RUNNING", "WAITING"]),
                col(SyncExecutionScope.status) == "PENDING",
            )
            .order_by(col(ReportSyncJob.created_at).desc())
        ).all():
            existing.setdefault(child.requirement_id, (job, child))
    reusable_ids = (
        {
            locked.id
            for locked in session.exec(
                select(ReportSyncJob)
                .where(
                    col(ReportSyncJob.id).in_({job.id for job, _ in existing.values()}),
                    and_(*_strictly_unstarted()),
                )
                .with_for_update(of=ReportSyncJob, skip_locked=True)
                .execution_options(populate_existing=True)
            ).all()
        }
        if existing
        else set()
    )
    scope_accounts = {scope.id: advertiser for advertiser, scope in scopes.items()}
    missing: dict[str, list[SyncRequirement]] = {
        advertiser: [] for advertiser in advertisers
    }
    for requirement in pending:
        candidate = existing.get(requirement.id)
        if candidate is not None:
            job, child = candidate
            # 只有今天/昨天的实时金额需从跨日历史任务拆出；补链和旧日期
            # 仍复用未启动执行。按tier误拆会在相邻归并时再次插入同一子桶。
            if job.id in reusable_ids and not (
                classes[scope_accounts[requirement.scope_id]] == "realtime"
                and job.start_date != job.end_date
            ):
                if child.frozen_revision < requirement.desired_revision:
                    if job.status == "WAITING":
                        child.observed_at = None  # 新需求必须由下一代真实观察确认。
                    else:
                        job.error_code = None
                child.frozen_revision = requirement.desired_revision
                child.frozen_requested_after = requirement.requested_after
                job.priority = min(job.priority, priority)
                # 等待证据的退避由真实依赖变化唤醒，周期需求不能使无变化重扫。
                if job.status != "WAITING":
                    job.due_at = min(job.due_at, due)
                due_by_requirement[requirement.id] = job.due_at
                continue
            if child.frozen_revision >= requirement.desired_revision:
                due_by_requirement[requirement.id] = job.due_at
                continue
        missing[scope_accounts[requirement.scope_id]].append(requirement)
    jobs: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    included: set[str] = set()
    epoch_groups = []
    for group in groups:
        for epoch in sorted({scopes[advertiser].owner_epoch for advertiser in group}):
            epoch_groups.append(
                [
                    advertiser
                    for advertiser in group
                    if scopes[advertiser].owner_epoch == epoch
                ]
            )
    for group in epoch_groups:
        uncovered = [
            advertiser
            for advertiser in group
            if missing[advertiser] and advertiser not in included
        ]
        if not uncovered:
            continue
        included.update(uncovered)
        for window_start, window_end, window_accounts in _missing_windows(
            missing,
            uncovered,
            max_days=1 if kind == "campaign_hour" else (execution_window_days or 30),
        ):
            merged = None
            if execution_window_days is None and all(
                classes[account] == "history" for account in window_accounts
            ):
                merged = _adjacent_unstarted(
                    session,
                    scope_ids=[scopes[account].id for account in window_accounts],
                    advertisers=window_accounts,
                    kind=kind,
                    start=window_start,
                    end=window_end,
                    cutoff=min(
                        now.astimezone(ZoneInfo(accounts[account].timezone)).date()
                        for account in window_accounts
                    ),
                )
            merged_children: dict[UUID, SyncExecutionScope] = {}
            if merged is not None:
                # 最新执行已启动时，missing仍可能被更早的未启动父承接。
                # 先检查全部旧引用（含终态），不能重复插入或复活已发布/被替代子桶。
                references = session.exec(
                    select(SyncExecutionScope)
                    .where(col(SyncExecutionScope.report_job_id) == merged.id)
                    .execution_options(populate_existing=True)
                ).all()
                allowed_scopes = {scopes[account].id for account in window_accounts}
                if any(
                    child.tenant_id != tenant_id
                    or child.scope_id not in allowed_scopes
                    or child.owner_epoch != merged.owner_epoch
                    or child.status != "PENDING"
                    or child.execution_generation != 0
                    or child.observed_at is not None
                    or child.published_version is not None
                    for child in references
                ):
                    merged = None
                else:
                    merged_children = {
                        child.requirement_id: child for child in references
                    }
            if merged is not None:
                merged.start_date, merged.end_date = (
                    min(merged.start_date, window_start),
                    max(merged.end_date, window_end),
                )
                merged.priority, merged.due_at = (
                    min(merged.priority, priority),
                    min(merged.due_at, due),
                )
                merged.progress = {"requested_at": requested.isoformat()}
                identity = merged.id
            else:
                identity = uuid4()
                job = ReportSyncJob(
                    id=identity,
                    owner_epoch=scopes[window_accounts[0]].owner_epoch,
                    tenant_id=tenant_id,
                    bc_id=bc_id,
                    kind=kind,
                    priority=priority,
                    advertiser_ids=window_accounts,
                    campaign_ids=list(campaign_ids),
                    start_date=window_start,
                    end_date=window_end,
                    job_key=f"sync-v2:{identity}",
                    frozen_route=frozen_route,
                    actor_id=actor_id,
                    due_at=due,
                    progress={
                        "requested_at": requested.isoformat(),
                        **(
                            {"execution_window_days": execution_window_days}
                            if execution_window_days is not None
                            else {}
                        ),
                    },
                )
                jobs.append(job.model_dump())
            for advertiser in window_accounts:
                scope = scopes[advertiser]
                for requirement in missing[advertiser]:
                    if (
                        not window_start
                        <= date.fromisoformat(requirement.bucket_key)
                        <= window_end
                    ):
                        continue
                    merged_child = merged_children.get(requirement.id)
                    if merged_child is not None:
                        # 只升级锁定且未启动执行的冻结需求；保留原引用ID及发布证据边界。
                        merged_child.frozen_revision = requirement.desired_revision
                        merged_child.frozen_requested_after = (
                            requirement.requested_after
                        )
                    else:
                        children.append(
                            SyncExecutionScope(
                                tenant_id=tenant_id,
                                scope_id=scope.id,
                                requirement_id=requirement.id,
                                report_job_id=identity,
                                frozen_revision=requirement.desired_revision,
                                frozen_requested_after=requirement.requested_after,
                                owner_epoch=scope.owner_epoch,
                                execution_generation=0,
                            ).model_dump()
                        )
                    due_by_requirement[requirement.id] = (
                        merged.due_at if merged is not None else due
                    )
    session.flush()
    if jobs:
        session.connection().execute(insert(ReportSyncJob), jobs)
    if children:
        session.connection().execute(insert(SyncExecutionScope), children)
    active_scopes: dict[tuple[UUID, str], datetime] = {}
    for row in pending:
        head_key = (row.scope_id, row.source)
        actual_due = due_by_requirement.get(row.id, due)
        active_scopes[head_key] = min(
            active_scopes.get(head_key, actual_due), actual_due
        )
    heads = [
        SyncSchedule(
            scope_id=scope_id,
            tenant_id=tenant_id,
            work_class=work_class,
            physical_endpoint=report_endpoint(kind),
            next_eligible_at=scope_due,
            oldest_pending_at=requested,
        ).model_dump()
        for (scope_id, work_class), scope_due in active_scopes.items()
    ]
    if heads:
        statement = insert(SyncSchedule)
        session.connection().execute(
            statement.on_conflict_do_update(
                index_elements=["scope_id", "work_class"],
                set_={
                    "active": True,
                    "next_eligible_at": func.least(
                        col(SyncSchedule.next_eligible_at),
                        statement.excluded.next_eligible_at,
                    ),
                    "oldest_pending_at": func.least(
                        col(SyncSchedule.oldest_pending_at),
                        statement.excluded.oldest_pending_at,
                    ),
                    # 新实时需求必须重新进入服务年龄队列；历史头的积压不能
                    # 让同一账户下一轮需求继续排在数千个旧实时头之后。
                    "last_dispatched_at": case(
                        (col(SyncSchedule.work_class) == "realtime", None),
                        else_=col(SyncSchedule.last_dispatched_at),
                    ),
                },
            ),
            heads,
        )
    return [row["id"] for row in jobs]


def adopt_report_job(session: Session, job: ReportSyncJob) -> int:
    """离线迁移原执行：不更换ID、不动页正文或游标、不发任何外部请求。"""
    if job.kind in {"material_day", "material_links"}:
        return 0
    existing = session.exec(
        select(col(SyncExecutionScope.id))
        .where(col(SyncExecutionScope.report_job_id) == job.id)
        .limit(1)
    ).first()
    if existing is not None:
        return 0
    requested = (
        datetime.fromisoformat(job.progress["requested_at"])
        if job.progress.get("requested_at")
        else job.created_at
    )
    paging = job.progress.get("basic_paging") or {}
    observed = paging.get("observed_at") or job.progress.get("snapshot_observed_at")
    observed = datetime.fromisoformat(observed) if observed else None
    revisions = []
    for advertiser in job.advertiser_ids:
        account = session.get(AdvertiserAccount, (job.tenant_id, advertiser))
        if account is None or not account.timezone:
            raise ValueError("legacy job has no authorized account timezone")
        scope = ensure_scope(
            session,
            route=job.frozen_route,
            actor_id=job.actor_id,
            advertiser_id=advertiser,
            resource=job.kind,
            query={"campaign_ids": sorted(job.campaign_ids)},
            timezone=account.timezone,
        )
        work_class = report_class(job.kind, job.priority)
        for offset in range((job.end_date - job.start_date).days + 1):
            day = job.start_date + timedelta(days=offset)
            revisions.append(
                merge_requirement(
                    session,
                    scope,
                    BucketKey(day.isoformat(), day),
                    requested_after=requested,
                    source=work_class,
                )
            )
        wake_scope(
            session,
            scope=scope,
            work_class=work_class,
            endpoint=report_endpoint(job.kind, job.progress),
            due_at=job.due_at,
            oldest_pending_at=job.created_at,
        )
    freeze_execution_scope(
        session,
        execution=job,
        execution_kind="report",
        requirements=revisions,
        observed_at=observed,
    )
    return len(revisions)
