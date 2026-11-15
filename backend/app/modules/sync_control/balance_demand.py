"""余额共用维护类需求、逐账户缺口和到期派发，不把末页遗漏当成零。"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any
from typing import cast as typing_cast
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import or_, text, true, update
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import AccountBalance
from app.integrations.tiktok.platform_quotas import sdk_endpoint
from app.jobs.admission import admission_policy
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.store_models import (
    ReportBalanceRun,
)
from app.modules.sync_control.contracts import ExecutionFence
from app.modules.sync_control.dispatch_budget import (
    DispatchBudgetConflict,
    EndpointBudgets,
    dispatch_budget_transaction,
)
from app.modules.sync_control.dispatch_envelope import enqueue_sync_dispatch
from app.modules.sync_control.fairness import due_tenants
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncSchedule,
    SyncScope,
)
from app.modules.sync_control.occupancy import release_dispatch, reserve_dispatch
from app.modules.sync_control.requirements import (
    execution_owner_current,
    freeze_execution_scope,
    merge_requirement,
    satisfy_requirements,
)
from app.modules.sync_control.schedule import wake_scope
from app.modules.sync_control.scopes import ensure_scope
from app.modules.sync_control.worker_pool import (
    reserve_background_turn,
)

ENDPOINT = sdk_endpoint("finance.get_advertiser_balances")


def adopt_balance_run(session: Session, run: ReportBalanceRun) -> None:
    """离线接管余额保留原剩余账户与页码。"""
    return register_balance(session, run)


def balance_scopes(
    session: Session,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    route: dict[str, Any],
    advertisers: Collection[str],
) -> dict[str, SyncScope]:
    result = {}
    for advertiser in sorted(set(advertisers)):
        account = session.get(AdvertiserAccount, (tenant_id, advertiser))
        if account is None:
            raise ValueError("balance requirement account is outside tenant scope")
        scope = ensure_scope(
            session,
            route=route,
            actor_id=actor_id,
            advertiser_id=advertiser,
            resource="balance",
            query={},
            timezone=account.timezone or "not-applicable",
            metric_contract="balance-v2",
        )
        session.refresh(scope, with_for_update=True)
        result[advertiser] = scope
    return result


def register_balance(
    session: Session,
    run: ReportBalanceRun,
    *,
    scopes: dict[str, SyncScope] | None = None,
) -> None:
    # 既有执行从run开始持锁；planner传入的scopes对应本事务刚创建的新run。
    session.flush()
    session.exec(
        select(col(ReportBalanceRun.id))
        .where(col(ReportBalanceRun.id) == run.id)
        .with_for_update()
    ).one()
    scopes = scopes or balance_scopes(
        session,
        tenant_id=run.tenant_id,
        actor_id=run.actor_id,
        route=run.frozen_route,
        advertisers=run.remaining_advertiser_ids,
    )
    for advertiser in run.remaining_advertiser_ids:
        scope = scopes[advertiser]
        revision = merge_requirement(
            session,
            scope,
            {"key": "current"},
            requested_after=run.created_at,
            source="maintenance",
        )
        freeze_execution_scope(
            session, execution=run, execution_kind="balance", requirements=[revision]
        )
        wake_scope(
            session,
            scope=scope,
            work_class="maintenance",
            endpoint=ENDPOINT,
            due_at=run.next_attempt_at or datetime.now(UTC),
            oldest_pending_at=run.created_at,
        )


def _lock_balance_execution(
    session: Session, run: ReportBalanceRun
) -> tuple[dict[UUID, SyncScope], list[SyncExecutionScope]]:
    # planner按账户顺序取得scope；所有worker/outcome须遵循同一顺序，
    # 不能以UUID排序或joint FOR UPDATE先锁child再等待planner持有的scope。
    session.flush()
    session.exec(
        select(col(ReportBalanceRun.id))
        .where(col(ReportBalanceRun.id) == run.id)
        .with_for_update()
    ).one()
    scopes = session.exec(
        select(SyncScope)
        .where(
            col(SyncScope.id).in_(
                select(col(SyncExecutionScope.scope_id)).where(
                    col(SyncExecutionScope.balance_run_id) == run.id
                )
            )
        )
        .order_by(
            col(SyncScope.tenant_id), col(SyncScope.advertiser_id), col(SyncScope.id)
        )
        .with_for_update(of=SyncScope)
        .execution_options(populate_existing=True)
    ).all()
    children = session.exec(
        select(SyncExecutionScope)
        .where(col(SyncExecutionScope.balance_run_id) == run.id)
        .order_by(col(SyncExecutionScope.scope_id), col(SyncExecutionScope.id))
        .with_for_update(of=SyncExecutionScope)
        .execution_options(populate_existing=True)
    ).all()
    return {scope.id: scope for scope in scopes}, list(children)


def balance_fence(session: Session, run: ReportBalanceRun) -> None:
    from app.core.errors import DomainError

    scopes, children = _lock_balance_execution(session, run)
    for child in children:
        scope = scopes[child.scope_id]
        if scope.owner_epoch != run.owner_epoch or child.owner_epoch != run.owner_epoch:
            raise DomainError("balance_claim_lost", "余额scope所有权已更换")
        if child.status != "PENDING":
            continue
        if child.execution_generation != run.claim_generation:
            raise DomainError("balance_claim_lost", "余额执行代数已更换")


def publish_balance_requirement(
    session: Session, run: ReportBalanceRun, advertiser: str, balance: AccountBalance
) -> bool:
    # adapter末页会补缺口观测及当前observed_at；它不等于真正读到余额。
    # 缺失/权限不确定仍可落观测，但不能推进需求水位；真实零余额属于有效数据。
    if (
        balance.availability != "AVAILABLE"
        or balance.amount is None
        or not balance.amount.is_finite()
    ):
        return False
    satisfy_requirements(
        session,
        fence=ExecutionFence(run.id, "balance", run.claim_generation, run.owner_epoch),
        observed_at=balance.observed_at,
        publication_version=max(1, run.claim_generation),
        advertiser_id=advertiser,
    )
    return (
        session.exec(
            select(col(SyncExecutionScope.id))
            .join(SyncScope, col(SyncScope.id) == col(SyncExecutionScope.scope_id))
            .where(
                col(SyncExecutionScope.balance_run_id) == run.id,
                col(SyncScope.advertiser_id) == advertiser,
                col(SyncExecutionScope.status) == "PUBLISHED",
            )
            .limit(1)
        ).first()
        is not None
    )


def balance_outcome(session: Session, run: ReportBalanceRun) -> None:
    from app.modules.sync_control.schedule import (
        pending_execution_dues,
        retire_execution_children,
    )

    _lock_balance_execution(session, run)
    if run.status in {"COMPLETE", "FAILED", "CANCELLED"} and run.completed_at is None:
        # 终态首次被可靠观测时起算90天；不能以创建时间倒推完成时间。
        run.completed_at = datetime.now(UTC)
    terminal = retire_execution_children(session, run, "balance")
    heads = session.exec(
        select(SyncSchedule)
        .join(
            SyncExecutionScope,
            col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
        )
        .where(col(SyncExecutionScope.balance_run_id) == run.id)
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .with_for_update(of=SyncSchedule)
        .execution_options(populate_existing=True)
    ).all()
    due_by_head = pending_execution_dues(
        session, heads, "balance", now=datetime.now(UTC)
    )
    for head in heads:
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
        if terminal and not head.active:
            head.wait_reason = terminal


def balance_reservation(run: ReportBalanceRun, generation: int) -> str:
    return f"balance:{run.id}:{generation}:{run.next_page}"


def dispatch_balances_due(
    session: Session, redis: Redis, *, now: datetime, limit: int = 2
) -> int:
    try:
        with dispatch_budget_transaction(session, redis) as budgets:
            return _dispatch_balances_due(
                session, redis, now=now, limit=limit, budgets=budgets
            )
    except DispatchBudgetConflict:
        return 0


def _dispatch_balances_due(
    session: Session,
    redis: Redis,
    *,
    now: datetime,
    limit: int = 2,
    budgets: EndpointBudgets,
) -> int:
    # 家族窗口独立串行；共享后台池仅在实际后台准入时非阻塞锁定。
    if (
        not session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:sync-v2-balance',0))"
            )
        )
        .scalar_one()
    ):
        return 0
    due = [
        col(SyncSchedule.active).is_(True),
        col(SyncSchedule.work_class) == "maintenance",
        col(SyncSchedule.next_eligible_at) <= now,
        or_(
            col(SyncSchedule.leased_until).is_(None),
            col(SyncSchedule.leased_until) <= now,
        ),
    ]
    tenants = due_tenants(session, due, limit=min(64, limit))
    count = 0
    for _ in range(limit):
        progressed = False
        for tenant_id in tenants:
            if count >= limit:
                break
            cursor = budgets.acquire(tenant_id, ENDPOINT)
            if cursor is None:
                continue
            # 先对轻量执行集合排序；LATERAL的LIMIT/行锁使整批owner核验只发生在
            # 实际探测的候选上，不把数千账户×几十周期展开后才取一个执行。
            candidates = (
                select(col(ReportBalanceRun.id).label("id"))
                .where(
                    col(ReportBalanceRun.tenant_id) == tenant_id,
                    col(ReportBalanceRun.status) == "ACTIVE",
                    or_(
                        col(ReportBalanceRun.claimed_until).is_(None),
                        col(ReportBalanceRun.claimed_until) <= now,
                    ),
                    or_(
                        col(ReportBalanceRun.next_attempt_at).is_(None),
                        col(ReportBalanceRun.next_attempt_at) <= now,
                    ),
                    select(col(SyncExecutionScope.id))
                    .join(
                        SyncSchedule,
                        col(SyncSchedule.scope_id) == col(SyncExecutionScope.scope_id),
                    )
                    .where(
                        *due,
                        col(SyncExecutionScope.balance_run_id)
                        == col(ReportBalanceRun.id),
                        col(SyncExecutionScope.status) == "PENDING",
                    )
                    .limit(1)
                    .offset(0)  # 保持逐执行探测，禁止优化器拉平成全关联扫描。
                    .correlate(ReportBalanceRun)
                    .exists(),
                )
                .order_by(col(ReportBalanceRun.created_at), col(ReportBalanceRun.id))
                .cte("balance_candidates")
                .prefix_with("MATERIALIZED")
            )
            locked = (
                select(ReportBalanceRun)
                .where(
                    col(ReportBalanceRun.id) == candidates.c.id,
                    execution_owner_current(ReportBalanceRun, "balance_run_id"),
                )
                .limit(1)
                .with_for_update(of=ReportBalanceRun, skip_locked=True)
                .correlate(candidates)
                .lateral("locked_balance")
            )
            selected = aliased(ReportBalanceRun, locked)
            run = session.exec(
                select(selected).select_from(candidates).join(locked, true()).limit(1)
            ).first()
            if run is None:
                continue
            head = session.exec(
                select(SyncSchedule)
                .join(
                    SyncExecutionScope,
                    col(SyncExecutionScope.scope_id) == col(SyncSchedule.scope_id),
                )
                .where(
                    *due,
                    col(SyncSchedule.tenant_id) == tenant_id,
                    col(SyncExecutionScope.balance_run_id) == run.id,
                    col(SyncExecutionScope.status) == "PENDING",
                )
                .order_by(
                    col(SyncSchedule.last_dispatched_at).asc().nullsfirst(),
                    col(SyncSchedule.oldest_pending_at),
                    col(SyncSchedule.scope_id),
                )
                .limit(1)
            ).first()
            if head is None:
                continue
            # 所有路径按run→child/scope→head顺序持锁，避免与发布/失败回写倒序竞争。
            balance_fence(session, run)
            generation = run.claim_generation + 1
            token = balance_reservation(run, generation)
            ready = reserve_dispatch(
                redis,
                reservation_id=token,
                policy=admission_policy(
                    "finance.get_advertiser_balances",
                    channel="OFFICIAL_API",
                    physical_endpoint=ENDPOINT,
                ),
                app_scope=settings.TIKTOK_APP_ID,
                endpoint=ENDPOINT,
                rate_group=ENDPOINT,
                tenant_id=run.tenant_id,
                advertiser_id=run.remaining_advertiser_ids[0],
            )
            if ready.capacity_hint == 0:
                head.next_eligible_at = now + timedelta(
                    milliseconds=max(1, ready.retry_after_ms)
                )
                head.wait_reason = ready.reason
                continue
            from app.modules.reporting.planner import backfill_allowed

            if not reserve_background_turn(
                session,
                redis,
                category="maintenance",
                now=now,
                allow_backfill=backfill_allowed(redis, now),
            ):
                release_dispatch(
                    redis,
                    reservation_id=token,
                    app_scope=settings.TIKTOK_APP_ID,
                    endpoint=ENDPOINT,
                    rate_group=ENDPOINT,
                    tenant_id=run.tenant_id,
                    advertiser_id=run.remaining_advertiser_ids[0],
                )
                continue
            run.claim_generation = generation
            run.claim_token = uuid4()
            run.claimed_until = now + timedelta(seconds=90)
            typing_cast(SQLAlchemySession, session).execute(
                update(SyncExecutionScope)
                .where(
                    col(SyncExecutionScope.balance_run_id) == run.id,
                    col(SyncExecutionScope.owner_epoch) == run.owner_epoch,
                    col(SyncExecutionScope.status) == "PENDING",
                )
                .values(execution_generation=generation)
            )
            # 先锁全部账户头再写派发租约；outcome也按同一顺序锁并刷新缓存。
            session.exec(
                select(SyncSchedule)
                .where(
                    col(SyncSchedule.scope_id).in_(
                        select(col(SyncExecutionScope.scope_id)).where(
                            col(SyncExecutionScope.balance_run_id) == run.id
                        )
                    ),
                    col(SyncSchedule.work_class) == "maintenance",
                )
                .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            head.lease_execution_id, head.lease_execution_generation = (
                run.id,
                generation,
            )
            head.lease_token, head.leased_until = uuid4(), now + timedelta(seconds=90)
            head.last_dispatched_at = now
            session.connection().execute(
                update(SyncSchedule)
                .where(
                    col(SyncSchedule.scope_id).in_(
                        select(col(SyncExecutionScope.scope_id)).where(
                            col(SyncExecutionScope.balance_run_id) == run.id
                        )
                    ),
                    col(SyncSchedule.work_class) == "maintenance",
                )
                .values(
                    lease_token=head.lease_token,
                    leased_until=head.leased_until,
                    lease_execution_id=run.id,
                    lease_execution_generation=generation,
                    last_dispatched_at=now,
                )
            )
            enqueue_sync_dispatch(
                session,
                execution=run,
                now=now,
                context=TenantContext(
                    tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
                ),
                task_name="reporting.balance_step",
                task_key=token,
                payload={
                    "run_id": str(run.id),
                    "page": run.next_page,
                    "claim_id": str(run.claim_token),
                    "claim_generation": generation,
                },
            )
            from app.modules.sync_control.costs import reserve_execution_cost
            from app.modules.sync_control.fairness import WEIGHTS, choose_work_class

            _, state = choose_work_class(
                cursor.fairness_state.get(ENDPOINT, {}), ["maintenance"]
            )
            reserve_execution_cost(
                session,
                cursor=cursor,
                execution=run,
                kind="balance",
                endpoint=ENDPOINT,
                work_class="maintenance",
                eligible_weights={"maintenance": WEIGHTS["maintenance"]},
                state_after_unit_charge=state,
                reserved_units=1,
                lease_until=run.claimed_until,
            )
            count += 1
            cursor.last_dispatched_at = now
            progressed = True
        if not progressed:
            break
    return count
