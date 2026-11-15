"""停消费者后原地接管旧任务；事务由调用者逐批提交，保留页、游标及冻结授权。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, exists
from sqlalchemy import cast as sql_cast
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.jobs.models import PendingDispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import ReportBalanceRun, ReportSyncJob
from app.modules.sync_control.models import SyncExecutionScope, SyncWorkerPool


@dataclass
class AdoptionResult:
    adopted: int = 0
    deferred: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    cursor: UUID | None = None
    scanned: int = 0


type Execution = ReportSyncJob | AdDirectoryRun | ReportBalanceRun


def configure_background_pool(
    session: Session,
    *,
    consumers_stopped: bool,
    capacity: int | None = None,
    prefetch_slots: int | None = None,
) -> SyncWorkerPool:
    """离线切换显式核对运行进程数和有界候选窗口，不重置既有公平游标。"""
    from sqlalchemy.dialects.postgresql import insert

    if not consumers_stopped:
        raise ValueError("consumers must be stopped before pool configuration")
    if capacity is not None and (
        type(capacity) is not int or not 1 <= capacity <= 1024
    ):
        raise ValueError("capacity must be between 1 and 1024")
    if prefetch_slots is not None and (
        type(prefetch_slots) is not int or not 0 <= prefetch_slots <= 1024
    ):
        raise ValueError("prefetch_slots must be between 0 and 1024")
    session.connection().execute(
        insert(SyncWorkerPool)
        .values(pool_name="background", capacity=4, prefetch_slots=0, fairness_state={})
        .on_conflict_do_nothing(index_elements=["pool_name"])
    )
    row = session.get(
        SyncWorkerPool, "background", with_for_update=True, populate_existing=True
    )
    assert row is not None
    if capacity is not None:
        row.capacity = capacity
    if prefetch_slots is not None:
        row.prefetch_slots = prefetch_slots
    session.flush()
    return row


def legacy_balance_dispatch_query() -> SelectOfScalar[PendingDispatch]:
    """仅识别精确旧载荷且已完成离线映射的余额消息；非法引用不做UUID转换。"""
    payload = col(PendingDispatch.payload)
    return (
        select(PendingDispatch)
        .join(
            ReportBalanceRun,
            (sql_cast(col(ReportBalanceRun.id), String) == payload["run_id"].astext)
            & (col(ReportBalanceRun.tenant_id) == col(PendingDispatch.tenant_id))
            & (col(ReportBalanceRun.actor_id) == col(PendingDispatch.actor_id)),
        )
        .where(
            col(PendingDispatch.task_name) == "reporting.balance_step",
            col(PendingDispatch.published_at).is_(None),
            col(PendingDispatch.superseded_at).is_(None),
            # 与待派发部分索引一致；忽略已退役消息，避免扫描整张历史派发表。
            col(PendingDispatch.retired_at).is_(None),
            payload.op("?")("run_id"),
            payload.op("?")("page"),
            payload.op("-")("run_id").op("-")("page") == {},
            col(ReportBalanceRun.contract_version) == 2,
            col(ReportBalanceRun.claim_token).is_(None),
            exists(
                select(col(SyncExecutionScope.id)).where(
                    col(SyncExecutionScope.balance_run_id) == col(ReportBalanceRun.id)
                )
            ),
        )
    )


def retire_legacy_balance_dispatches(
    session: Session, *, consumers_stopped: bool, limit: int = 100
) -> int:
    """全停切换中逐批退役无领取围栏的旧消息，原页和幂等身份均保留。"""
    if not consumers_stopped:
        raise ValueError("legacy consumers must be stopped before supersession")
    if not 1 <= limit <= 1000:
        raise ValueError("batch limit must be between 1 and 1000")
    rows = session.exec(
        legacy_balance_dispatch_query()
        .order_by(col(PendingDispatch.id))
        .limit(limit)
        .with_for_update(of=PendingDispatch)
    ).all()
    for row in rows:
        row.superseded_at = datetime.now(UTC)
    session.flush()
    return len(rows)


def adoption_query(kind: str) -> tuple[type[Execution], SelectOfScalar[Execution]]:
    """只扫尚未映射的活动执行；已有子桶由正常恢复流程处理，不能重复接管。"""
    model: type[Execution]
    statuses: tuple[str, ...]
    if kind == "report":
        model, child = ReportSyncJob, col(SyncExecutionScope.report_job_id)
        statuses = ("QUEUED", "RUNNING", "WAITING")
    elif kind == "directory":
        model, child = AdDirectoryRun, col(SyncExecutionScope.directory_run_id)
        statuses = ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")
    elif kind == "balance":
        model, child = ReportBalanceRun, col(SyncExecutionScope.balance_run_id)
        statuses = ("ACTIVE",)
    else:
        raise ValueError("kind must be report, directory or balance")
    return model, select(model).where(
        col(model.status).in_(statuses),
        ~exists(select(col(SyncExecutionScope.id)).where(child == col(model.id))),
    )


def adopt_legacy_batch(
    session: Session,
    *,
    kind: str,
    consumers_stopped: bool,
    now: datetime | None = None,
    after_id: UUID | None = None,
    limit: int = 100,
) -> AdoptionResult:
    """仅离线切换可调用；有效租约须等待到期，不冒充已经完成正常退出。"""
    if not consumers_stopped:
        raise ValueError("legacy consumers must be stopped before adoption")
    if not 1 <= limit <= 1000:
        raise ValueError("batch limit must be between 1 and 1000")
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    model, statement = adoption_query(kind)
    if after_id is not None:
        statement = statement.where(col(model.id) > after_id)
    # 离线独占迁移，不用SKIP LOCKED跳过仍在写入的旧任务。
    rows = session.exec(
        statement.order_by(col(model.id)).limit(limit).with_for_update()
    ).all()
    result = AdoptionResult()
    for row in rows:
        result.cursor = row.id
        result.scanned += 1
        if row.claimed_until is not None and row.claimed_until > now:
            result.deferred += 1
            continue
        try:
            with session.begin_nested():
                row.claim_token = None
                row.claimed_until = None
                row.claim_generation += 1
                row.contract_version = 2
                if kind == "report":
                    from app.modules.sync_control.report_demand import adopt_report_job

                    assert isinstance(row, ReportSyncJob)
                    row.status = "WAITING"
                    adopt_report_job(session, row)
                elif kind == "directory":
                    from app.modules.sync_control.directory_demand import (
                        register_directory_run,
                    )

                    assert isinstance(row, AdDirectoryRun)
                    row.status = "QUEUED"
                    register_directory_run(session, row)
                else:
                    from app.modules.sync_control.balance_demand import (
                        adopt_balance_run,
                    )

                    assert isinstance(row, ReportBalanceRun)
                    adopt_balance_run(session, row)
                # 不允许“接管成功”却没有任何可恢复子桶。
                child_column = {
                    "report": col(SyncExecutionScope.report_job_id),
                    "directory": col(SyncExecutionScope.directory_run_id),
                    "balance": col(SyncExecutionScope.balance_run_id),
                }[kind]
                session.flush()
                if (
                    session.exec(
                        select(col(SyncExecutionScope.id))
                        .where(child_column == row.id)
                        .limit(1)
                    ).first()
                    is None
                ):
                    raise ValueError("legacy execution has no pending scope")
            result.adopted += 1
        except (ValueError, AttributeError) as exc:
            # 只记录任务ID和异常类型；冻结授权或页正文可能包含敏感数据。
            result.errors.append({"id": str(row.id), "code": type(exc).__name__})
    return result
