"""目录分区也使用同一需求/到期头；页正文仍留在ad_directory_page。"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.integrations.tiktok.platform_quotas import sdk_endpoint
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)
from app.modules.sync_control.requirements import (
    freeze_execution_scope,
    merge_requirement,
)
from app.modules.sync_control.schedule import wake_scope
from app.modules.sync_control.scopes import ensure_scope


def _refresh_scope_ages(session: Session, scope_ids: Collection[UUID]) -> None:
    from app.modules.sync_control.directory_pending_age import (
        refresh_directory_pending_age,
    )

    session.flush()
    heads = session.exec(
        select(SyncSchedule).where(col(SyncSchedule.scope_id).in_(scope_ids))
    ).all()
    refresh_directory_pending_age(session, heads)


def promote_directory_heads(session: Session, run_ids: Collection[UUID]) -> None:
    """状态入口批量晋升已证明未开始的目录需求，SQL往返不随对象数增长。"""
    from sqlalchemy import func, update
    from sqlalchemy.dialects.postgresql import insert

    if not run_ids:
        return
    references = select(col(SyncExecutionScope.requirement_id)).where(
        col(SyncExecutionScope.directory_run_id).in_(run_ids),
        col(SyncExecutionScope.status) == "PENDING",
    )
    typing_cast(SQLAlchemySession, session).execute(
        update(SyncRequirement)
        .where(
            col(SyncRequirement.id).in_(references),
            col(SyncRequirement.satisfied_revision)
            < col(SyncRequirement.desired_revision),
        )
        .values(source="realtime")
    )
    heads = session.exec(
        select(SyncSchedule)
        .join(
            SyncExecutionScope,
            col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
        )
        .where(
            col(SyncExecutionScope.directory_run_id).in_(run_ids),
            col(SyncSchedule.work_class) == "directory",
        )
    ).all()
    values = []
    for head in {row.scope_id: row for row in heads}.values():
        values.append(
            SyncSchedule(
                scope_id=head.scope_id,
                tenant_id=head.tenant_id,
                work_class="realtime",
                physical_endpoint=head.physical_endpoint,
                next_eligible_at=head.next_eligible_at,
                oldest_pending_at=head.oldest_pending_at,
            ).model_dump()
        )
    if values:
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
                },
            ),
            values,
        )
        remaining = (
            select(col(SyncExecutionScope.id))
            .join(
                SyncRequirement,
                col(SyncRequirement.id) == col(SyncExecutionScope.requirement_id),
            )
            .where(
                col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
                col(SyncExecutionScope.status) == "PENDING",
                col(SyncRequirement.source) == "directory",
                col(SyncRequirement.satisfied_revision)
                < col(SyncRequirement.desired_revision),
            )
            .correlate(SyncSchedule)
            .exists()
        )
        typing_cast(SQLAlchemySession, session).execute(
            update(SyncSchedule)
            .where(
                col(SyncSchedule.scope_id).in_([row["scope_id"] for row in values]),
                col(SyncSchedule.work_class) == "directory",
            )
            .values(active=remaining)
        )
        _refresh_scope_ages(session, [row["scope_id"] for row in values])


def directory_endpoint(run: AdDirectoryRun) -> str:
    if run.kind == "creative":
        # Smart+创意明细及素材子页均由官方ad/get读取；资产组才走smart_plus/ad/get。
        return sdk_endpoint("ads.get_ads")
    if run.ad_type == "SMART_PLUS":
        return sdk_endpoint(
            {
                "campaign": "build.get_campaigns",
                "adgroup": "build.get_adgroups",
                "ad": "build.get_ads",
            }[run.kind]
        )
    return sdk_endpoint(
        {
            "campaign": "ads.get_campaigns",
            "adgroup": "build.get_regular_adgroups",
            "ad": "ads.get_ads",
        }[run.kind]
    )


def directory_scope(session: Session, run: AdDirectoryRun) -> SyncScope:
    from app.modules.ads.directory_calibration import PERIODIC_KEY
    from app.modules.ads.directory_services import SERVICE_KEY

    account = session.get(AdvertiserAccount, (run.tenant_id, run.advertiser_id))
    if account is None:
        raise ValueError("directory requirement account is outside tenant scope")
    query = {
        key: value
        for key, value in run.query.items()
        if key not in {"page", "ids", "parent_ids", PERIODIC_KEY, SERVICE_KEY}
    }
    return ensure_scope(
        session,
        route=run.frozen_route,
        actor_id=run.actor_id,
        advertiser_id=run.advertiser_id,
        resource=f"directory:{run.kind}:{run.ad_type}",
        query=query,
        timezone=account.timezone or "not-applicable",
        metric_contract="directory-v2",
    )


def register_directory_run(
    session: Session, run: AdDirectoryRun, *, new_execution: bool = False
) -> None:
    from app.modules.ads.directory_services import directory_service

    existing = session.exec(
        select(SyncExecutionScope).where(
            col(SyncExecutionScope.directory_run_id) == run.id
        )
    ).first()
    if existing is not None:
        requirement = session.get(SyncRequirement, existing.requirement_id)
        assert requirement is not None  # execution FK保证引用存在
        if (
            directory_service(run) == "status"
            and requirement.source != "realtime"
            and requirement.satisfied_revision < requirement.desired_revision
        ):
            from app.modules.sync_control.schedule import has_pending_execution

            requirement.source = "realtime"
            scope = session.get(SyncScope, existing.scope_id)
            assert scope is not None
            wake_scope(
                session,
                scope=scope,
                work_class="realtime",
                endpoint=directory_endpoint(run),
                due_at=run.next_attempt_at or datetime.now(UTC),
                oldest_pending_at=requirement.oldest_pending_at,
            )
            old_head = session.get(SyncSchedule, (scope.id, "directory"))
            if old_head is not None:
                old_head.active = has_pending_execution(session, old_head)
            _refresh_scope_ages(session, [scope.id])
        return
    scope = directory_scope(session, run)
    if new_execution:
        # 仅创建入口可继承当前owner；恢复、离线迁移和旧消息绝不改写已冻结代数。
        session.refresh(scope, with_for_update=True)
        run.owner_epoch = scope.owner_epoch
    work_class = "realtime" if directory_service(run) == "status" else "directory"
    revision = merge_requirement(
        session,
        scope,
        {"key": run.partition_key, "query": run.query},
        requested_after=run.created_at,
        source=work_class,
    )
    requirement = session.get(SyncRequirement, revision.requirement_id)
    assert requirement is not None
    work_class = requirement.source
    freeze_execution_scope(
        session, execution=run, execution_kind="directory", requirements=[revision]
    )
    wake_scope(
        session,
        scope=scope,
        work_class=work_class,
        endpoint=directory_endpoint(run),
        due_at=run.next_attempt_at or datetime.now(UTC),
        oldest_pending_at=run.created_at,
    )
    _refresh_scope_ages(session, [scope.id])


def directory_fence(
    session: Session,
    run: AdDirectoryRun,
    *,
    claim_token: UUID | None = None,
    claim: bool = False,
) -> None:
    from app.core.errors import DomainError

    if claim_token is not None and run.claim_token != claim_token:
        raise DomainError("directory_claim_lost", "目录执行租约已更换")
    for child, scope in session.exec(
        select(SyncExecutionScope, SyncScope)
        .join(SyncScope, col(SyncScope.id) == col(SyncExecutionScope.scope_id))
        .where(col(SyncExecutionScope.directory_run_id) == run.id)
        .with_for_update()
    ).all():
        if scope.owner_epoch != run.owner_epoch or child.owner_epoch != run.owner_epoch:
            raise DomainError("directory_claim_lost", "目录scope执行所有权已更换")
        if claim:
            child.execution_generation = run.claim_generation
            child.observed_at = (
                child.observed_at or run.observed_at or datetime.now(UTC)
            )
        elif child.execution_generation != run.claim_generation:
            raise DomainError("directory_claim_lost", "目录子桶执行代数已失效")


def handoff_directory_publication(
    session: Session, run: AdDirectoryRun, *, now: datetime
) -> bool:
    """末页在发布窗口有余量时沿原grant交接，满时由规范发布调度恢复。"""
    from app.modules.ads.directory_dispatch import enqueue_directory_step
    from app.modules.sync_control.directory_dispatch import (
        publication_handoff_available,
    )

    # HTTP开始时有资格，不代表返回时原head仍有效；锁不到或已失效就保留
    # 已暂存末页，由既有到期头规范恢复，不能让签发失败回滚有效平台数据。
    head = session.exec(
        select(SyncSchedule)
        .where(
            col(SyncSchedule.tenant_id) == run.tenant_id,
            col(SyncSchedule.lease_token).is_not(None),
            col(SyncSchedule.lease_execution_id) == run.id,
            col(SyncSchedule.lease_execution_generation) == run.claim_generation,
            col(SyncSchedule.leased_until) > now,
        )
        .limit(1)
        .with_for_update(of=SyncSchedule, skip_locked=True)
    ).first()
    if head is None:
        return False
    # 两阶段拆分后，直接交接也必须遵守发布容量；不能用原读取grant绕过上限。
    # try-lock失败只交还已暂存的末页，不在Worker里等待公共调度锁。
    if not publication_handoff_available(session, now=now):
        return False
    # 与grant检查使用同一签发时点；page、合法消息和原grant同事务保留。
    enqueue_directory_step(
        session, run, publish_only=True, dispatch_ready=True, dispatched_at=now
    )
    return True


def directory_outcome(
    session: Session, run: AdDirectoryRun, *, expected_lease_token: UUID | None = None
) -> None:
    from app.modules.sync_control.schedule import (
        pending_execution_dues,
        retire_execution_children,
    )

    terminal = retire_execution_children(session, run, "directory")
    heads = session.exec(
        select(SyncSchedule)
        .join(
            SyncExecutionScope,
            col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
        )
        .where(col(SyncExecutionScope.directory_run_id) == run.id)
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .with_for_update(of=SyncSchedule)
        .execution_options(populate_existing=True)
    ).all()
    due_by_head = pending_execution_dues(
        session, heads, "directory", now=datetime.now(UTC)
    )
    from app.modules.sync_control.directory_pending_age import (
        refresh_directory_pending_age,
    )

    # 即使不能清掉另一代grant，当前年龄缓存也只反映尚未满足的执行。
    refresh_directory_pending_age(session, heads)
    for head in heads:
        if (
            expected_lease_token is not None
            and head.lease_token is not None
            and head.lease_token != expected_lease_token
        ):
            continue  # 页间资格变化不能清除后来签发的grant，即使执行ID/代数相同。
        if head.lease_execution_id is not None and (
            head.lease_execution_id != run.id
            or head.lease_execution_generation != run.claim_generation
        ):
            continue
        head.lease_token = head.leased_until = head.lease_execution_id = (
            head.lease_execution_generation
        ) = None
        due = due_by_head.get((head.scope_id, head.work_class))
        head.active = due is not None
        if due is not None:
            head.next_eligible_at = due
        head.wait_reason = "QUOTA" if run.error_code == "admission_deferred" else None
        if terminal and not head.active:
            head.wait_reason = terminal
