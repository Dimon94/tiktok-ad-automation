"""一账户一桶只有一份当前需求；执行只确认读取前冻结的 revision。"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import case, func, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import ReportBalanceRun, ReportSyncJob
from app.modules.sync_control.contracts import (
    BucketKey,
    ExecutionFence,
    RequirementRevision,
)
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncScope,
)


def pending_work_class(source: str | ColumnElement[str]) -> ColumnElement[str]:
    # 较低优先级的新请求可以增强新鲜度，不能降级尚未完成的实时需求。
    return case(
        (
            (
                col(SyncRequirement.satisfied_revision)
                < col(SyncRequirement.desired_revision)
            )
            & (col(SyncRequirement.source) == "realtime"),
            col(SyncRequirement.source),
        ),
        else_=source,
    )


def merge_requirement(
    session: Session,
    scope: SyncScope,
    bucket: BucketKey | dict[str, Any],
    *,
    requested_after: datetime,
    deadline: datetime | None = None,
    source: str,
) -> RequirementRevision:
    if requested_after.utcoffset() is None or (
        deadline is not None and deadline.utcoffset() is None
    ):
        raise ValueError("requirement timestamps must include timezone")
    if isinstance(bucket, BucketKey):
        key = bucket.key
        value = (
            {"local_date": bucket.local_date.isoformat()} if bucket.local_date else {}
        )
    else:
        key, value = bucket["key"], dict(bucket)
    row = SyncRequirement(
        tenant_id=scope.tenant_id,
        scope_id=scope.id,
        bucket_key=key,
        bucket=value,
        requested_after=requested_after,
        deadline=deadline,
        oldest_pending_at=requested_after,
        source=source,
    )
    statement = insert(SyncRequirement).values(**row.model_dump())
    newer = statement.excluded.requested_after > col(SyncRequirement.requested_after)
    returning = statement.on_conflict_do_update(
        index_elements=["scope_id", "bucket_key"],
        set_={
            "desired_revision": col(SyncRequirement.desired_revision)
            + case((newer, 1), else_=0),
            "requested_after": func.greatest(
                col(SyncRequirement.requested_after), statement.excluded.requested_after
            ),
            "deadline": func.least(
                col(SyncRequirement.deadline), statement.excluded.deadline
            ),
            "oldest_pending_at": case(
                (
                    (
                        col(SyncRequirement.satisfied_revision)
                        == col(SyncRequirement.desired_revision)
                    )
                    & newer,
                    requested_after,
                ),
                else_=col(SyncRequirement.oldest_pending_at),
            ),
            "source": pending_work_class(source),
        },
    ).returning(SyncRequirement)
    current = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            select(SyncRequirement)
            .from_statement(returning)
            .execution_options(populate_existing=True)
        )
        .scalar_one()
    )
    return RequirementRevision(
        current.id, current.scope_id, current.desired_revision, current.requested_after
    )


def freeze_execution_scope(
    session: Session,
    *,
    execution: ReportSyncJob | ReportBalanceRun | AdDirectoryRun,
    execution_kind: str,
    requirements: Sequence[RequirementRevision],
    observed_at: datetime | None = None,
) -> ExecutionFence:

    models = {
        "report": ReportSyncJob,
        "directory": AdDirectoryRun,
        "balance": ReportBalanceRun,
    }
    columns = {
        "report": "report_job_id",
        "directory": "directory_run_id",
        "balance": "balance_run_id",
    }
    if execution_kind not in models or not isinstance(
        execution, models[execution_kind]
    ):
        raise ValueError("execution reference does not match its persisted type")
    generation = getattr(execution, "claim_generation", 0)
    epoch = getattr(execution, "owner_epoch", 1)
    column = columns[execution_kind]
    for revision in requirements:
        requirement = session.get(
            SyncRequirement, revision.requirement_id, with_for_update=True
        )
        scope = session.get(SyncScope, revision.scope_id)
        advertisers = (
            execution.advertiser_ids
            if isinstance(execution, ReportSyncJob)
            else execution.remaining_advertiser_ids
            if isinstance(execution, ReportBalanceRun)
            else [execution.advertiser_id]
        )
        if (
            scope is None
            or requirement is None
            or requirement.scope_id != scope.id
            or execution.tenant_id != scope.tenant_id
            or execution.actor_id != scope.actor_id
            or scope.advertiser_id not in advertisers
            or scope.owner_epoch != epoch
        ):
            raise ValueError("execution cannot freeze a different authorization scope")
        reference_values: dict[str, Any] = {column: execution.id}
        row = SyncExecutionScope(
            tenant_id=scope.tenant_id,
            scope_id=scope.id,
            requirement_id=requirement.id,
            **reference_values,
            frozen_revision=revision.revision,
            frozen_requested_after=revision.requested_after,
            owner_epoch=epoch,
            execution_generation=generation,
            observed_at=observed_at,
        )
        statement = (
            insert(SyncExecutionScope)
            .values(**row.model_dump())
            .on_conflict_do_nothing(
                index_elements=[column, "requirement_id"],
                index_where=getattr(SyncExecutionScope, column).is_not(None),
            )
        )
        session.connection().execute(statement)
    return ExecutionFence(execution.id, execution_kind, generation, epoch)


def satisfy_requirements(
    session: Session,
    *,
    fence: ExecutionFence,
    observed_at: datetime,
    publication_version: int | Mapping[tuple[str, str], int],
    bucket_keys: Collection[str] | None = None,
    advertiser_id: str | None = None,
) -> list[UUID]:
    column = {
        "report": col(SyncExecutionScope.report_job_id),
        "directory": col(SyncExecutionScope.directory_run_id),
        "balance": col(SyncExecutionScope.balance_run_id),
    }[fence.execution_kind]
    statement = (
        select(SyncExecutionScope, SyncRequirement, SyncScope)
        .join(
            SyncRequirement,
            col(SyncExecutionScope.requirement_id) == col(SyncRequirement.id),
        )
        .join(SyncScope, col(SyncExecutionScope.scope_id) == col(SyncScope.id))
        .where(column == fence.execution_id)
    )
    # 余额逐账户、报表逐日桶发布时，范围必须在SQL领取锁之前生效。
    # 取回整批再Python过滤会重复读取/锁定其它账户，批量回写退化为平方级。
    if advertiser_id is not None:
        statement = statement.where(col(SyncScope.advertiser_id) == advertiser_id)
    if bucket_keys is not None:
        statement = statement.where(col(SyncRequirement.bucket_key).in_(bucket_keys))
    # 报表批量发布携带每个实际写入桶的版本。必须按账户/日期成对过滤，
    # 不能取两个集合的笛卡尔积，否则会误确认被新水位保护、没有写入的桶。
    if isinstance(publication_version, Mapping):
        if not publication_version:
            return []
        statement = statement.where(
            tuple_(col(SyncScope.advertiser_id), col(SyncRequirement.bucket_key)).in_(
                sorted(publication_version)
            )
        )
    rows = session.exec(statement.with_for_update()).all()
    successors = []
    for child, requirement, scope in rows:
        if (
            child.owner_epoch != scope.owner_epoch
            or child.owner_epoch != fence.owner_epoch
        ):
            continue
        if (
            child.execution_generation != fence.generation
            or observed_at < child.frozen_requested_after
        ):
            continue
        if child.status == "PUBLISHED":
            continue
        # 只证明冻结的版本；执行开始后到达的刷新绝不会被旧回包吞掉。
        child.status, child.observed_at, child.published_version = (
            "PUBLISHED",
            observed_at,
            publication_version[(scope.advertiser_id, requirement.bucket_key)]
            if isinstance(publication_version, Mapping)
            else publication_version,
        )
        requirement.satisfied_revision = max(
            requirement.satisfied_revision, child.frozen_revision
        )
        if requirement.satisfied_revision == requirement.desired_revision:
            requirement.oldest_pending_at = None
        else:
            successors.append(requirement.id)
    session.flush()
    return successors


def execution_owner_current(
    model: type[ReportSyncJob] | type[ReportBalanceRun] | type[AdDirectoryRun],
    reference_column: str,
) -> ColumnElement[bool]:
    """一个批次任何子scope失去owner都不能领取，不能只检查选中那个头。"""
    from sqlalchemy import or_
    from sqlalchemy.orm import aliased

    child, scope = aliased(SyncExecutionScope), aliased(SyncScope)
    return (
        ~select(child.id)
        .join(scope, col(scope.id) == col(child.scope_id))
        .where(
            getattr(child, reference_column) == col(model.id),
            or_(
                col(child.owner_epoch) != col(scope.owner_epoch),
                col(model.owner_epoch) != col(scope.owner_epoch),
            ),
        )
        .correlate(model)
        .exists()
    )
