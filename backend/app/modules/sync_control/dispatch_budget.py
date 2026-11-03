"""按物理接口串行公平决策；租户JSON行只在领取事务末尾短锁合并。"""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis import Redis
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.occupancy import reservation_transaction

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _directory_policy_key(endpoint: str) -> str:
    # 独立root键：成本结算只处理物理接口余额，不接触调度机会/扫描状态。
    return f"__directory_policy__:{endpoint}"


class DispatchBudgetConflict(Exception):
    """同接口竞争或旧写入者改变余额；当前家族领取必须整体撤销。"""


@dataclass
class _Budget:
    base: dict[str, float]
    dispatched_at: datetime
    cursor: ReportDispatchCursor
    policy_base: dict[str, float]
    policy: dict[str, float]


class EndpointBudgets:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._items: dict[tuple[UUID, str], _Budget | None] = {}
        self._policy_views: dict[UUID, dict[str, dict[str, float]]] = {}
        self.settled = 0

    def peek_directory_policy(self, tenant_id: UUID, endpoint: str) -> dict[str, float]:
        item = self._items.get((tenant_id, endpoint))
        if item is not None:
            return deepcopy(item.policy)
        if tenant_id not in self._policy_views:
            stored = self.session.get(
                ReportDispatchCursor, tenant_id, populate_existing=True
            )
            self._policy_views[tenant_id] = deepcopy(
                stored.fairness_state if stored else {}
            )
        return deepcopy(
            self._policy_views[tenant_id].get(_directory_policy_key(endpoint), {})
        )

    def directory_policy(self, tenant_id: UUID, endpoint: str) -> dict[str, float]:
        item = self._items.get((tenant_id, endpoint))
        if item is None:
            raise ValueError("directory policy requires the acquired endpoint budget")
        return item.policy

    def acquire(self, tenant_id: UUID, endpoint: str) -> ReportDispatchCursor | None:
        key = (tenant_id, endpoint)
        if key in self._items:
            entry = self._items[key]
            return entry.cursor if entry else None
        if (
            not self.session.connection()
            .execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"tkada:sync-budget:{tenant_id}:{endpoint}"},
            )
            .scalar_one()
        ):
            self._items[key] = None
            return None
        stored = self.session.get(
            ReportDispatchCursor, tenant_id, populate_existing=True
        )
        base = deepcopy(stored.fairness_state.get(endpoint, {}) if stored else {})
        policy_base = deepcopy(
            stored.fairness_state.get(_directory_policy_key(endpoint), {})
            if stored
            else {}
        )
        dispatched = stored.last_dispatched_at if stored else _EPOCH
        # 不入Session，后续成本结算/领取不会提前UPDATE并占住整个租户行。
        cursor = ReportDispatchCursor(
            tenant_id=tenant_id,
            last_dispatched_at=dispatched,
            fairness_state={endpoint: deepcopy(base)},
        )
        self._items[key] = _Budget(
            base, dispatched, cursor, policy_base, deepcopy(policy_base)
        )
        from app.modules.sync_control.costs import settle_costs

        self.settled += settle_costs(self.session, cursor, endpoint=endpoint)
        return cursor

    def persist(self) -> None:
        grouped: dict[UUID, dict[str, _Budget]] = {}
        for (tenant_id, endpoint), item in self._items.items():
            if item is not None and (
                item.cursor.fairness_state.get(endpoint, {}) != item.base
                or item.cursor.last_dispatched_at != item.dispatched_at
                or item.policy != item.policy_base
            ):
                grouped.setdefault(tenant_id, {})[endpoint] = item
        # 成本/租约/outbox先准备；最后按统一租户顺序短锁，跨接口不能覆盖彼此的键。
        for tenant_id, items in sorted(grouped.items()):
            stored = self.session.exec(
                select(ReportDispatchCursor)
                .where(ReportDispatchCursor.tenant_id == tenant_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ).one_or_none()
            if stored is None:
                exists = self.session.exec(
                    select(col(ReportDispatchCursor.tenant_id)).where(
                        ReportDispatchCursor.tenant_id == tenant_id
                    )
                ).first()
                if (
                    exists is not None
                    or not self.session.connection()
                    .execute(
                        text(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"
                        ),
                        {"key": f"tkada:sync-cursor-init:{tenant_id}"},
                    )
                    .scalar_one()
                ):
                    raise DispatchBudgetConflict()
                self.session.connection().execute(
                    insert(ReportDispatchCursor)
                    .values(
                        tenant_id=tenant_id,
                        last_dispatched_at=_EPOCH,
                        fairness_state={},
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id"])
                )
                stored = self.session.get(
                    ReportDispatchCursor,
                    tenant_id,
                    with_for_update={"skip_locked": True},
                    populate_existing=True,
                )
                if stored is None:
                    raise DispatchBudgetConflict()
            # 旧版本或其它写入者可能在本次只读快照后已提交；绝不以旧同键覆盖它。
            if any(
                stored.fairness_state.get(ep, {}) != item.base
                or stored.fairness_state.get(_directory_policy_key(ep), {})
                != item.policy_base
                for ep, item in items.items()
            ):
                raise DispatchBudgetConflict()
            updates = {
                ep: item.cursor.fairness_state[ep]
                for ep, item in items.items()
                if item.cursor.fairness_state.get(ep, {}) != item.base
                or item.cursor.last_dispatched_at != item.dispatched_at
            }
            updates.update(
                {
                    _directory_policy_key(ep): item.policy
                    for ep, item in items.items()
                    if item.policy != item.policy_base
                }
            )
            stored.fairness_state = stored.fairness_state | updates
            stored.last_dispatched_at = max(
                stored.last_dispatched_at,
                *(item.cursor.last_dispatched_at for item in items.values()),
            )
        self.session.flush()


@contextmanager
def dispatch_budget_transaction(
    session: Session, redis: Redis
) -> Iterator[EndpointBudgets]:
    # Redis在SAVEPOINT之外：保存点提交或最终外层COMMIT失败都必须撤销queued预留。
    with reservation_transaction(redis), session.begin_nested():
        budgets = EndpointBudgets(session)
        yield budgets
        budgets.persist()
