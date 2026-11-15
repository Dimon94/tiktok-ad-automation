"""Tenant-scoped daily plan transactions."""

from datetime import UTC, date, datetime
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerStrategyBinding,
    AIOptimizerVersion,
    BuildOccurrence,
)
from app.modules.ai_workbench.plan_schemas import (
    DailyPlanPage,
    DailyPlanPublic,
    DailyPlanRequest,
    DailyPlanRowInput,
    DailyPlanRowPublic,
    parse_plan_tsv,
    validate_plan_rows,
)
from app.modules.ai_workbench.plans import AIDailyPlan, AIDailyPlanRow
from app.modules.providers.models import ProviderApplication
from app.modules.tenants.permissions import require_tenant


def _authorize(
    session: Session, context: TenantContext, *, write: bool = False
) -> None:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="strategy_write" if write else "read",
    )


def _optimizer(
    session: Session, context: TenantContext, optimizer_id: UUID
) -> AIOptimizer:
    row = session.exec(
        select(AIOptimizer).where(
            col(AIOptimizer.tenant_id) == context.tenant_id,
            col(AIOptimizer.id) == optimizer_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
    return row


def _optimizer_context(
    session: Session, context: TenantContext, optimizer_id: UUID
) -> AIOptimizer:
    optimizer = _optimizer(session, context, optimizer_id)
    if not optimizer.active:
        raise DomainError("ai_optimizer_inactive", "ai_optimizer_inactive")
    bindings = session.exec(
        select(AIOptimizerStrategyBinding).where(
            col(AIOptimizerStrategyBinding.tenant_id) == context.tenant_id,
            col(AIOptimizerStrategyBinding.optimizer_id) == optimizer_id,
            col(AIOptimizerStrategyBinding.active).is_(True),
        )
    ).all()
    if len(bindings) != 1:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    return optimizer


def _request_rows(body: DailyPlanRequest) -> list[DailyPlanRowInput]:
    try:
        rows = (
            parse_plan_tsv(body.tsv) if body.tsv is not None else list(body.rows or [])
        )
        return validate_plan_rows(rows)
    except (TypeError, ValueError) as error:
        raise DomainError("ai_daily_plan_invalid", str(error)) from error


def _public(session: Session, plan: AIDailyPlan) -> DailyPlanPublic:
    rows = session.exec(
        select(AIDailyPlanRow)
        .where(
            col(AIDailyPlanRow.tenant_id) == plan.tenant_id,
            col(AIDailyPlanRow.plan_id) == plan.id,
        )
        .order_by(col(AIDailyPlanRow.position))
    ).all()
    from app.modules.ai_workbench.orders import MaterialOrder

    order_rows = set(
        session.exec(
            select(col(MaterialOrder.plan_row_id)).where(
                col(MaterialOrder.tenant_id) == plan.tenant_id,
                col(MaterialOrder.optimizer_id) == plan.optimizer_id,
            )
        ).all()
    )
    frozen = set(
        session.exec(
            select(col(BuildOccurrence.plan_row_id)).where(
                col(BuildOccurrence.tenant_id) == plan.tenant_id,
                col(BuildOccurrence.optimizer_id) == plan.optimizer_id,
            )
        ).all()
    )
    return DailyPlanPublic(
        id=plan.id,
        tenant_id=plan.tenant_id,
        optimizer_id=plan.optimizer_id,
        business_date=plan.business_date,
        revision=plan.revision,
        rows=[
            DailyPlanRowPublic.model_validate(row, from_attributes=True).model_copy(
                update={"read_only": row.id in frozen or row.id in order_rows}
            )
            for row in rows
        ],
    )


def _validate_rows(rows: list[DailyPlanRowInput], *, optimizer_id: UUID) -> date:
    if any(row.optimizer_id != optimizer_id for row in rows):
        raise DomainError(
            "ai_daily_plan_optimizer_mismatch", "ai_daily_plan_optimizer_mismatch"
        )
    dates = {row.business_date for row in rows}
    if len(dates) > 1:
        raise DomainError("ai_daily_plan_day_mismatch", "ai_daily_plan_day_mismatch")
    # Empty replacements use the date supplied by the existing parent, so the
    # caller handles this case before invoking this helper.
    if not dates:
        raise DomainError("ai_daily_plan_date_required", "ai_daily_plan_date_required")
    return next(iter(dates))


def _validate_provider_scope(
    session: Session, *, context: TenantContext, rows: list[DailyPlanRowInput]
) -> None:
    for row in rows:
        application = session.exec(
            select(ProviderApplication).where(
                col(ProviderApplication.tenant_id) == context.tenant_id,
                col(ProviderApplication.connection_id) == row.provider_connection_id,
                col(ProviderApplication.external_id) == row.application_id,
            )
        ).one_or_none()
        if application is None:
            raise DomainError(
                "ai_provider_application_not_found",
                "ai_provider_application_not_found",
            )


def create_daily_plan(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    body: DailyPlanRequest,
) -> DailyPlanPublic:
    _authorize(session, context, write=True)
    _optimizer_context(session, context, optimizer_id)
    rows = _request_rows(body)
    _validate_provider_scope(session, context=context, rows=rows)
    business_date = body.business_date or _validate_rows(
        rows, optimizer_id=optimizer_id
    )
    if rows and _validate_rows(rows, optimizer_id=optimizer_id) != business_date:
        raise DomainError("ai_daily_plan_day_mismatch", "ai_daily_plan_day_mismatch")
    existing = session.exec(
        select(AIDailyPlan).where(
            col(AIDailyPlan.tenant_id) == context.tenant_id,
            col(AIDailyPlan.optimizer_id) == optimizer_id,
            col(AIDailyPlan.business_date) == business_date,
        )
    ).one_or_none()
    if existing is not None:
        raise DomainError("ai_daily_plan_exists", "ai_daily_plan_exists")
    plan = AIDailyPlan(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer_id,
        business_date=business_date,
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(plan)
    session.flush()
    _replace_rows(session, plan, rows)
    session.flush()
    return _public(session, plan)


def update_daily_plan(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    business_date: date | None = None,
    body: DailyPlanRequest,
) -> DailyPlanPublic:
    _authorize(session, context, write=True)
    _optimizer_context(session, context, optimizer_id)
    rows = _request_rows(body)
    _validate_provider_scope(session, context=context, rows=rows)
    requested_date = business_date or body.business_date
    if requested_date is None and rows:
        requested_date = _validate_rows(rows, optimizer_id=optimizer_id)
    if requested_date is None:
        raise DomainError("ai_daily_plan_date_required", "ai_daily_plan_date_required")
    plan = session.exec(
        select(AIDailyPlan)
        .where(
            col(AIDailyPlan.tenant_id) == context.tenant_id,
            col(AIDailyPlan.optimizer_id) == optimizer_id,
            col(AIDailyPlan.business_date) == requested_date,
        )
        .with_for_update()
    ).one_or_none()
    if plan is None:
        raise DomainError("ai_daily_plan_not_found", "ai_daily_plan_not_found")
    if body.revision != plan.revision:
        raise DomainError(
            "ai_daily_plan_revision_conflict", "ai_daily_plan_revision_conflict"
        )
    if rows and _validate_rows(rows, optimizer_id=optimizer_id) != requested_date:
        raise DomainError("ai_daily_plan_day_mismatch", "ai_daily_plan_day_mismatch")
    plan.revision += 1
    plan.updated_by = context.actor_id
    plan.updated_at = datetime.now(plan.updated_at.tzinfo)
    session.add(plan)
    _replace_rows(session, plan, rows)
    session.flush()
    return _public(session, plan)


def get_daily_plan(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    business_date: date,
) -> DailyPlanPublic:
    _authorize(session, context)
    _optimizer_context(session, context, optimizer_id)
    plan = session.exec(
        select(AIDailyPlan).where(
            col(AIDailyPlan.tenant_id) == context.tenant_id,
            col(AIDailyPlan.optimizer_id) == optimizer_id,
            col(AIDailyPlan.business_date) == business_date,
        )
    ).one_or_none()
    if plan is None:
        raise DomainError("ai_daily_plan_not_found", "ai_daily_plan_not_found")
    return _public(session, plan)


def list_daily_plans(
    session: Session, *, context: TenantContext, optimizer_id: UUID
) -> DailyPlanPage:
    _authorize(session, context)
    _optimizer_context(session, context, optimizer_id)
    plans = session.exec(
        select(AIDailyPlan)
        .where(
            col(AIDailyPlan.tenant_id) == context.tenant_id,
            col(AIDailyPlan.optimizer_id) == optimizer_id,
        )
        .order_by(col(AIDailyPlan.business_date).desc())
    ).all()
    return DailyPlanPage(
        items=[_public(session, plan) for plan in plans], total=len(plans)
    )


def _replace_rows(
    session: Session, plan: AIDailyPlan, rows: list[DailyPlanRowInput]
) -> None:
    # 保留行标识及发生记录引用；已经冻结的行不可更改或移除，避免重复搭建。
    existing = session.exec(
        select(AIDailyPlanRow)
        .where(
            col(AIDailyPlanRow.tenant_id) == plan.tenant_id,
            col(AIDailyPlanRow.plan_id) == plan.id,
        )
        .with_for_update()
    ).all()
    by_id = {row.id: row for row in existing}
    from app.modules.ai_workbench.orders import MaterialOrder

    order_rows = set(
        session.exec(
            select(col(MaterialOrder.plan_row_id)).where(
                col(MaterialOrder.tenant_id) == plan.tenant_id,
                col(MaterialOrder.optimizer_id) == plan.optimizer_id,
            )
        ).all()
    )
    frozen = set(
        session.exec(
            select(col(BuildOccurrence.plan_row_id)).where(
                col(BuildOccurrence.tenant_id) == plan.tenant_id,
                col(BuildOccurrence.optimizer_id) == plan.optimizer_id,
            )
        ).all()
    ) & set(by_id)
    frozen |= order_rows & set(by_id)
    supplied = [row.id for row in rows if row.id is not None]
    if len(supplied) != len(set(supplied)) or any(
        row_id not in by_id for row_id in supplied
    ):
        raise DomainError("ai_daily_plan_row_invalid", "排期行不属于当前剧单或重复")
    if not frozen.issubset(supplied):
        raise DomainError("ai_daily_plan_row_frozen", "已进入执行的排期行不能移除")
    for incoming in rows:
        if incoming.id is not None and incoming.id in frozen:
            saved = by_id[incoming.id]
            if any(
                getattr(saved, name) != value
                for name, value in incoming.model_dump(exclude={"id"}).items()
            ):
                raise DomainError(
                    "ai_daily_plan_row_frozen", "已进入执行的排期行不能修改"
                )
    optimizer = _optimizer(
        session,
        TenantContext(tenant_id=plan.tenant_id, actor_id=plan.updated_by, role=""),
        plan.optimizer_id,
    )
    version = session.exec(
        select(AIOptimizerVersion).where(
            col(AIOptimizerVersion.tenant_id) == plan.tenant_id,
            col(AIOptimizerVersion.optimizer_id) == plan.optimizer_id,
            col(AIOptimizerVersion.number) == optimizer.latest_version,
        )
    ).one()
    # 先释放位置唯一约束，允许用户对尚未执行的行重新排序。
    for saved in existing:
        saved.position += 1001
        session.add(saved)
    session.flush()
    for saved in existing:
        if saved.id not in supplied:
            session.delete(saved)
    session.flush()
    for position, incoming in enumerate(rows):
        saved = by_id.get(incoming.id)
        if saved is None:
            saved = AIDailyPlanRow(
                tenant_id=plan.tenant_id,
                plan_id=plan.id,
                **incoming.model_dump(exclude={"id"}),
                position=position,
                timezone=version.timezone,
            )
        else:
            if saved.id not in frozen:
                for name, value in incoming.model_dump(exclude={"id"}).items():
                    setattr(saved, name, value)
                saved.timezone = version.timezone
                saved.next_dispatch_at = datetime(1970, 1, 1, tzinfo=UTC)
                saved.last_dispatch_error = None
            saved.position = position
        session.add(saved)
