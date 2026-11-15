"""真实物理发送成本：worker只写收据，调度器按cursor→cost顺序结算。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import and_, func, literal, or_, union_all
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import (
    ReportBalanceRun,
    ReportDispatchCursor,
    ReportSyncJob,
)
from app.modules.sync_control.fairness import WEIGHTS
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule

Execution = ReportSyncJob | AdDirectoryRun | ReportBalanceRun
REFERENCES = {
    "report": "report_job_id",
    "directory": "directory_run_id",
    "balance": "balance_run_id",
}


def apply_cost_delta(
    state: Mapping[str, float], weights: Mapping[str, int], work_class: str, units: int
) -> dict[str, float]:
    """按物理单位推进全部冻结份额，而不是每个Celery步骤只推进一次。"""
    if work_class not in weights or any(
        key not in WEIGHTS or value <= 0 for key, value in weights.items()
    ):
        raise ValueError("cost requires a valid frozen eligibility vector")
    result = {key: float(state.get(key, 0)) for key in WEIGHTS}
    for key, weight in weights.items():
        result[key] += weight * units
    result[work_class] -= sum(weights.values()) * units
    return result


def reserve_execution_cost(
    session: Session,
    *,
    cursor: ReportDispatchCursor,
    execution: Execution,
    kind: str,
    endpoint: str,
    work_class: str,
    eligible_weights: Mapping[str, int],
    state_after_unit_charge: Mapping[str, float],
    reserved_units: int,
    lease_until: datetime,
) -> SyncCostLedger:
    if (
        execution.tenant_id != cursor.tenant_id
        or reserved_units < 0
        or execution.claim_generation < 1
    ):
        raise ValueError("cost reservation must match a claimed tenant execution")
    reference = REFERENCES[kind]
    existing = session.exec(
        select(SyncCostLedger).where(
            getattr(SyncCostLedger, reference) == execution.id,
            col(SyncCostLedger.generation) == execution.claim_generation,
            col(SyncCostLedger.physical_endpoint) == endpoint,
        )
    ).one_or_none()
    if existing is not None:
        return existing
    vectors: dict[str, dict[str, int]] = {}
    for other_endpoint, other_class in session.exec(
        select(col(SyncSchedule.physical_endpoint), col(SyncSchedule.work_class))
        .where(
            col(SyncSchedule.tenant_id) == execution.tenant_id,
            col(SyncSchedule.active).is_(True),
        )
        .distinct()
    ).all():
        vectors.setdefault(other_endpoint, {})[other_class] = WEIGHTS[other_class]
    vectors[endpoint] = dict(eligible_weights)
    reference_values: dict[str, Any] = {reference: execution.id}
    ledger = SyncCostLedger(
        tenant_id=execution.tenant_id,
        **reference_values,
        generation=execution.claim_generation,
        physical_endpoint=endpoint,
        work_class=work_class,
        eligible_weights=dict(eligible_weights),
        endpoint_weights=vectors,
        reserved_units=reserved_units,
        charged_units=reserved_units,
        lease_until=lease_until,
    )
    session.add(ledger)
    cursor.fairness_state = cursor.fairness_state | {
        endpoint: apply_cost_delta(
            state_after_unit_charge, eligible_weights, work_class, reserved_units - 1
        )
    }
    return ledger


def record_cost_receipt(
    session: Session,
    ledger_id: UUID,
    producer_id: UUID,
    *,
    total_units: int,
    closed: bool,
) -> None:
    """每worker累计max只增加真实发送差额；多消费者不能共用一个计数器互相遮挡。"""
    if total_units < 0:
        raise ValueError("physical attempts cannot be negative")
    ledger = session.get(
        SyncCostLedger, ledger_id, with_for_update=True, populate_existing=True
    )
    if ledger is None:
        return  # 已超过父任务保留窗口；合法执行在清理前有独立保护。
    producers = dict(ledger.producers)
    key = str(producer_id)
    before = producers.get(key, {"units": 0, "closed": False})
    after = {
        "units": max(int(before["units"]), total_units),
        "closed": bool(before["closed"] or closed),
    }
    if key in producers and after == before:
        return
    producers[key] = after
    ledger.producers = producers
    ledger.observed_units += after["units"] - int(before["units"])
    ledger.closed = all(item["closed"] for item in producers.values())
    ledger.receipt_version += 1
    ledger.updated_at = datetime.now(UTC)


def _settlement_due() -> ColumnElement[bool]:
    # 在途且仍被原预留覆盖时，没有要改的公平成本，不锁其收据阻塞worker收尾。
    # 完结/封存必须结算版本；超过已扣成本的真实发送仍立即补扣，未知成本不退款。
    return and_(
        col(SyncCostLedger.receipt_version) > col(SyncCostLedger.settled_version),
        or_(
            col(SyncCostLedger.closed).is_(True),
            col(SyncCostLedger.abandoned_at).is_not(None),
            func.greatest(
                col(SyncCostLedger.reserved_units), col(SyncCostLedger.observed_units)
            )
            > col(SyncCostLedger.charged_units),
        ),
    )


def settle_costs(
    session: Session,
    cursor: ReportDispatchCursor,
    *,
    limit: int = 256,
    endpoint: str | None = None,
) -> int:
    """生产调用方先持接口advisory并使用独立快照；不读取/锁job，末尾才合并游标。"""
    session.flush()
    rows = session.exec(
        select(SyncCostLedger)
        .where(
            col(SyncCostLedger.tenant_id) == cursor.tenant_id,
            _settlement_due(),
            *(
                [col(SyncCostLedger.physical_endpoint) == endpoint]
                if endpoint is not None
                else []
            ),
        )
        # 匹配(tenant, endpoint, id)脏收据索引；端点间成本独立，避免空结算扫全历史主键。
        .order_by(col(SyncCostLedger.physical_endpoint), col(SyncCostLedger.id))
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    states = dict(cursor.fairness_state)
    for row in rows:
        # 硬崩溃或仍在网络中的执行没有完整收据，保留预留成本，不以超时伪退款。
        target = (
            max(row.charged_units, row.reserved_units, row.observed_units)
            if row.abandoned_at is not None
            else row.observed_units
            if row.closed
            else max(row.reserved_units, row.observed_units)
        )
        states[row.physical_endpoint] = apply_cost_delta(
            states.get(row.physical_endpoint, {}),
            row.eligible_weights,
            row.work_class,
            target - row.charged_units,
        )
        row.charged_units, row.settled_version = target, row.receipt_version
        row.updated_at = datetime.now(UTC)
    if rows:
        cursor.fairness_state = states
    return len(rows)


def cost_classes(
    session: Session, *, tenant_id: UUID, endpoint: str, now: datetime
) -> tuple[set[str], set[str]]:
    # 每次探测仍读取最新active/inflight，只把两次往返合为一次；不缓存到
    # 整个dispatch，否则新ledger和worker收据关闭会被旧快照遮住。
    rows = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            union_all(
                select(col(SyncSchedule.work_class), literal(False).label("inflight"))
                .where(
                    col(SyncSchedule.tenant_id) == tenant_id,
                    col(SyncSchedule.physical_endpoint) == endpoint,
                    col(SyncSchedule.active).is_(True),
                )
                .distinct(),
                select(col(SyncCostLedger.work_class), literal(True).label("inflight"))
                .where(
                    col(SyncCostLedger.tenant_id) == tenant_id,
                    col(SyncCostLedger.physical_endpoint) == endpoint,
                    col(SyncCostLedger.closed).is_(False),
                    col(SyncCostLedger.lease_until) > now,
                )
                .distinct(),
            )
        )
        .all()
    )
    return {work_class for work_class, _ in rows}, {
        work_class for work_class, inflight in rows if inflight
    }


class _LedgerObserver:
    """SDK线程只增内存计数；返回父线程后用独立短事务持久化。"""

    def __init__(self, database_engine: Engine, primary: SyncCostLedger) -> None:
        from threading import Lock
        from uuid import uuid4

        self.engine, self.primary = database_engine, primary
        self.producer_id = uuid4()
        self.lock = Lock()
        self.counts = {primary.physical_endpoint: 0}
        self.ledger_ids = {primary.physical_endpoint: primary.id}
        self.acknowledged: dict[str, tuple[int, bool]] = {}
        self.finished = False

    def attempt(self, endpoint: str) -> None:
        with self.lock:
            self.counts[endpoint] = self.counts.get(endpoint, 0) + 1

    def prepare(self, endpoint: str) -> None:
        # 新物理接口也须先留下unknown屏障，不能发完才首次登记。
        if endpoint in self.ledger_ids:
            return
        with self.lock:
            self.counts.setdefault(endpoint, 0)
        self.flush(required=True)

    def flush(self, *, required: bool = False) -> None:
        import logging

        from sqlalchemy import text
        from sqlalchemy.dialects.postgresql import insert

        with self.lock:
            counts = dict(self.counts)
            finished = self.finished
        # 一个producer只记已提交的累计快照；重复flush不应再等待调度器结算的行锁。
        # 新接口与新增发送仍逐项登记，失败事务绝不能提前推进确认水位。
        pending = {
            endpoint: total
            for endpoint, total in counts.items()
            if self.acknowledged.get(endpoint) != (total, finished)
        }
        if not pending:
            return
        committed_ids = dict(self.ledger_ids)
        try:
            with Session(self.engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL lock_timeout='500ms'"))
                session.connection().execute(text("SET LOCAL statement_timeout='2s'"))
                for endpoint, total in pending.items():
                    identity = committed_ids.get(endpoint)
                    if identity is None:
                        reference = next(
                            name
                            for name in REFERENCES.values()
                            if getattr(self.primary, name) is not None
                        )
                        weights = self.primary.endpoint_weights.get(endpoint, {}) | {
                            self.primary.work_class: WEIGHTS[self.primary.work_class]
                        }
                        row = SyncCostLedger(
                            tenant_id=self.primary.tenant_id,
                            **{reference: getattr(self.primary, reference)},
                            generation=self.primary.generation,
                            physical_endpoint=endpoint,
                            work_class=self.primary.work_class,
                            eligible_weights=weights,
                            endpoint_weights=self.primary.endpoint_weights,
                            lease_until=self.primary.lease_until,
                            reserved_units=1,
                        )
                        session.connection().execute(
                            insert(SyncCostLedger)
                            .values(row.model_dump())
                            .on_conflict_do_nothing(
                                index_elements=[
                                    reference,
                                    "generation",
                                    "physical_endpoint",
                                ],
                                index_where=getattr(SyncCostLedger, reference).is_not(
                                    None
                                ),
                            )
                        )
                        identity = session.exec(
                            select(col(SyncCostLedger.id)).where(
                                getattr(SyncCostLedger, reference)
                                == getattr(self.primary, reference),
                                col(SyncCostLedger.generation)
                                == self.primary.generation,
                                col(SyncCostLedger.physical_endpoint) == endpoint,
                            )
                        ).one()
                        committed_ids[endpoint] = identity
                    record_cost_receipt(
                        session,
                        identity,
                        self.producer_id,
                        total_units=total,
                        closed=finished,
                    )
            # UUID缓存只有在提交完成后才可见；回滚后下一次flush必须重新UPSERT。
            self.ledger_ids = committed_ids
            self.acknowledged.update(
                {endpoint: (total, finished) for endpoint, total in pending.items()}
            )
        except Exception:
            if required:
                raise  # 首次producer未持久化时禁止HTTP，原head租约保留给规范恢复。
            # 记账失败保留unknown预留，不能阻断/伪成功业务发送；累计值仍留在
            # 当前worker，下一物理边界或正常结束会重试同一producer累计收据。
            logging.getLogger(__name__).warning(
                "sync cost receipt deferred", exc_info=True
            )


def execution_cost_context(
    database_engine: Engine,
    *,
    tenant_id: UUID,
    execution_id: UUID,
    generation: int,
    kind: str,
) -> AbstractContextManager[None]:
    from contextlib import contextmanager

    @contextmanager
    def context() -> Iterator[None]:
        from app.integrations.tiktok.attempt_observer import (
            bind_attempt_observer,
            current_attempt_observer,
        )
        from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS

        if current_attempt_observer() is not None:
            yield
            return
        reference = REFERENCES[kind]
        with Session(database_engine, expire_on_commit=False) as session:
            primary = session.exec(
                select(SyncCostLedger)
                .where(
                    col(SyncCostLedger.tenant_id) == tenant_id,
                    getattr(SyncCostLedger, reference) == execution_id,
                    col(SyncCostLedger.generation) == generation,
                )
                .order_by(
                    col(SyncCostLedger.reserved_units).desc(), col(SyncCostLedger.id)
                )
                .limit(1)
            ).one_or_none()
        if primary is None:
            yield
            return
        observer = _LedgerObserver(database_engine, primary)
        observer.flush(
            required=True
        )  # 在发送前登记producer；仍不触碰cursor或业务job锁。
        completed = False
        with bind_attempt_observer(observer):
            try:
                yield
            except SDK_SCOPE_INTERRUPTS:
                raise  # 可能仍有SDK线程，不能把中断当作完整收据退款。
            except BaseException:
                completed = True
                raise
            else:
                completed = True
            finally:
                observer.finished = completed
                observer.flush()

    return context()


def finish_current_cost() -> None:
    from app.integrations.tiktok.attempt_observer import current_attempt_observer

    observer = current_attempt_observer()
    if isinstance(observer, _LedgerObserver):
        observer.finished = True
        observer.flush()


def abandon_expired_costs(session: Session, *, now: datetime, limit: int = 256) -> int:
    """失效代且父/head均无活租约才封存unknown；不反向锁业务运行。"""
    session.flush()
    eligible_parents = []
    for model, reference in (
        (ReportSyncJob, "report_job_id"),
        (AdDirectoryRun, "directory_run_id"),
        (ReportBalanceRun, "balance_run_id"),
    ):
        eligible_parents.append(
            select(col(model.id))
            .where(
                col(model.id) == getattr(SyncCostLedger, reference),
                or_(
                    col(model.claim_generation) > col(SyncCostLedger.generation),
                    col(model.status).in_(["COMPLETE", "FAILED", "CANCELLED", "STALE"]),
                ),
                or_(
                    col(model.claimed_until).is_(None), col(model.claimed_until) <= now
                ),
            )
            .correlate(SyncCostLedger)
            .exists()
        )
    active_head = (
        select(col(SyncSchedule.scope_id))
        .where(
            col(SyncSchedule.lease_execution_id)
            == func.coalesce(
                col(SyncCostLedger.report_job_id),
                col(SyncCostLedger.directory_run_id),
                col(SyncCostLedger.balance_run_id),
            ),
            col(SyncSchedule.leased_until) > now,
        )
        .correlate(SyncCostLedger)
        .exists()
    )
    # 资格必须在LIMIT前判定；否则最老32个未失效执行会永远挡住后面的终态成本。
    rows = session.exec(
        select(SyncCostLedger)
        .where(
            col(SyncCostLedger.closed).is_(False),
            col(SyncCostLedger.abandoned_at).is_(None),
            col(SyncCostLedger.lease_until) <= now,
            or_(*eligible_parents),
            ~active_head,
        )
        .order_by(col(SyncCostLedger.lease_until), col(SyncCostLedger.id))
        .limit(limit)
        .with_for_update(of=SyncCostLedger, skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for row in rows:
        row.abandoned_at, row.updated_at = now, now
        row.receipt_version += 1
    return len(rows)


def settle_idle_costs(
    database_engine: Engine, *, limit: int = 32, budget_seconds: float = 0.5
) -> int:
    """无后继需求时也结算完整收据；只取脏租户，每户独立有界事务。"""
    from time import monotonic

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    stop, total = monotonic() + budget_seconds, 0
    try:
        with Session(database_engine) as session, session.begin():
            session.connection().execute(text("SET LOCAL statement_timeout='500ms'"))
            session.connection().execute(text("SET LOCAL lock_timeout='100ms'"))
            abandon_expired_costs(session, now=datetime.now(UTC), limit=limit)
    except DBAPIError:
        pass  # 维护回滚不阻止后续收据或正常调度；未知状态仍保留，下轮再试。
    try:
        with Session(database_engine) as session, session.begin():
            session.connection().execute(text("SET LOCAL statement_timeout='500ms'"))
            endpoints = session.exec(
                select(
                    col(SyncCostLedger.tenant_id), col(SyncCostLedger.physical_endpoint)
                )
                .where(_settlement_due())
                .distinct()
                .limit(limit)
            ).all()
    except DBAPIError:
        return total  # 连候选发现都失败时让出维护预算，dispatch/outbox仍继续。
    from app.modules.sync_control.dispatch_budget import (
        DispatchBudgetConflict,
        EndpointBudgets,
    )

    for tenant_id, endpoint in endpoints:
        if monotonic() >= stop:
            break
        try:
            with Session(database_engine) as session, session.begin():
                session.connection().execute(
                    text("SET LOCAL statement_timeout='500ms'")
                )
                session.connection().execute(text("SET LOCAL lock_timeout='100ms'"))
                budgets = EndpointBudgets(session)
                if budgets.acquire(tenant_id, endpoint) is not None:
                    budgets.persist()
            total += budgets.settled
        except DBAPIError, DispatchBudgetConflict:
            continue  # 脏版本未推进，下次Beat继续，不伪造结算成功。
    return total
