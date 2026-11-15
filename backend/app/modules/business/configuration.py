"""商业费率与归属：按当前权限核验，修改推进计算围栏并留下审计。"""

from datetime import date
from decimal import Decimal
from typing import Any
from typing import cast as typing_cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.models import TenantBC
from app.modules.business.models import (
    BusinessMapping,
    BusinessRateSchedule,
    BusinessScope,
    utcnow,
)
from app.modules.business.rate_periods import (
    RateOperation,
    RatePeriod,
    period_rate,
    transform_periods,
)
from app.modules.business.schemas import RateInput, RatePreviewPublic, RatePublic
from app.modules.providers.models import ProviderConnection, ProviderDrama
from app.modules.reporting.filters import authorized_grants
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant


def scope_accounts(session: Session, context: TenantContext, bc_id: str) -> list[str]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise HTTPException(403, "当前BC不可访问")
    return sorted(
        {
            g.advertiser_id
            for g in authorized_grants(session, context=context, bc_id=bc_id)
        }
    )


def bump_scopes(session: Session, tenant_id: UUID, bc_id: str | None = None) -> None:
    statement = update(BusinessScope).where(col(BusinessScope.tenant_id) == tenant_id)
    if bc_id is not None:
        statement = statement.where(col(BusinessScope.bc_id) == bc_id)
    typing_cast(SQLAlchemySession, session).execute(
        statement.values(
            revision=col(BusinessScope.revision) + 1,
            last_daily=None,
            next_check_at=utcnow(),
        )
    )


def _rate_parent(
    session: Session,
    context: TenantContext,
    kind: str,
    scope_id: str,
    *,
    lock: bool = False,
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    statement: Any
    if kind == "provider":
        try:
            provider_id = UUID(scope_id)
        except ValueError:
            raise HTTPException(422, "版权方编号无效") from None
        statement = select(ProviderConnection).where(
            col(ProviderConnection.tenant_id) == context.tenant_id,
            col(ProviderConnection.id) == provider_id,
        )
    elif kind == "rebate":
        statement = select(TenantBC).where(
            col(TenantBC.tenant_id) == context.tenant_id,
            col(TenantBC.bc_id) == scope_id,
            col(TenantBC.ownership_conflict) == False,  # noqa: E712
        )
    else:
        raise HTTPException(422, "未知费率类别")
    if lock:
        statement = statement.with_for_update(key_share=True)
    if session.exec(statement).first() is None:
        raise HTTPException(404, "当前租户配置对象不存在或不可访问")


def latest_rate_schedule(
    session: Session, tenant_id: UUID, kind: str, scope_id: str
) -> BusinessRateSchedule | None:
    return session.exec(
        select(BusinessRateSchedule)
        .where(
            col(BusinessRateSchedule.tenant_id) == tenant_id,
            col(BusinessRateSchedule.kind) == kind,
            col(BusinessRateSchedule.scope_id) == scope_id,
        )
        .order_by(col(BusinessRateSchedule.revision).desc())
        .limit(1)
    ).first()


def read_rate_schedule(
    session: Session, *, context: TenantContext, kind: str, scope_id: str
) -> RatePublic:
    _rate_parent(session, context, kind, scope_id)
    row = latest_rate_schedule(session, context.tenant_id, kind, scope_id)
    return RatePublic(
        revision=row.revision if row else 0,
        periods=[RatePeriod.model_validate(p) for p in row.periods] if row else [],
    )


def rate_on(
    session: Session, tenant_id: UUID, kind: str, scope_id: str, day: date
) -> Decimal | None:
    row = latest_rate_schedule(session, tenant_id, kind, scope_id)
    return period_rate(
        [RatePeriod.model_validate(p) for p in row.periods] if row else [], day
    )


def rate_snapshot(session: Session, tenant_id: UUID, bc_id: str) -> dict[str, Any]:
    rows = session.exec(
        select(BusinessRateSchedule)
        .where(
            col(BusinessRateSchedule.tenant_id) == tenant_id,
            (col(BusinessRateSchedule.kind) == "provider")
            | (col(BusinessRateSchedule.bc_id) == bc_id),
        )
        .distinct(col(BusinessRateSchedule.kind), col(BusinessRateSchedule.scope_id))
        .order_by(
            col(BusinessRateSchedule.kind),
            col(BusinessRateSchedule.scope_id),
            col(BusinessRateSchedule.revision).desc(),
        )
    ).all()
    return {
        f"{r.kind}:{r.scope_id}": {"revision": r.revision, "periods": r.periods}
        for r in rows
    }


def change_rate(
    session: Session, *, context: TenantContext, body: RateInput, save: bool
) -> RatePreviewPublic:
    _rate_parent(session, context, body.kind, body.scope_id, lock=save)
    row = latest_rate_schedule(session, context.tenant_id, body.kind, body.scope_id)
    revision = row.revision if row else 0
    if body.expected_revision != revision:
        raise HTTPException(409, "比例配置已被修改，请刷新并重新预览")
    before = [RatePeriod.model_validate(p) for p in row.periods] if row else []
    operation = RateOperation.model_validate(
        body.model_dump(exclude={"kind", "scope_id", "expected_revision"})
    )
    try:
        periods = transform_periods(before, operation)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if save:
        row = BusinessRateSchedule(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            kind=body.kind,
            scope_id=body.scope_id,
            provider_id=UUID(body.scope_id) if body.kind == "provider" else None,
            bc_id=body.scope_id if body.kind == "rebate" else None,
            revision=revision + 1,
            periods=[p.model_dump(mode="json") for p in periods],
            operation=operation.model_dump(mode="json"),
        )
        session.add(row)
        bump_scopes(session, context.tenant_id, row.bc_id)
        session.add(
            AuditEvent(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                action="business.rate.update",
                target_id=str(row.id),
                details={
                    "kind": body.kind,
                    "scope_id": body.scope_id,
                    "revision": row.revision,
                    "before": [p.model_dump(mode="json") for p in before],
                    "after": row.periods,
                    "operation": row.operation,
                },
            )
        )
        session.flush()
        revision += 1
    return RatePreviewPublic(revision=revision, before=before, periods=periods)


def save_rate(session: Session, *, context: TenantContext, **values: Any) -> RatePublic:
    result = change_rate(session, context=context, body=RateInput(**values), save=True)
    return RatePublic(revision=result.revision, periods=result.periods)


def save_mapping(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    campaign_id: str,
    drama_id: UUID,
) -> BusinessMapping:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if advertiser_id not in scope_accounts(session, context, bc_id):
        raise DomainError("action_forbidden", "当前BC未授权该账户")
    drama = session.exec(
        select(ProviderDrama).where(
            col(ProviderDrama.tenant_id) == context.tenant_id,
            col(ProviderDrama.id) == drama_id,
        )
    ).first()
    if drama is None:
        raise DomainError("resource_not_found", "当前租户剧目不存在")
    # BC非键锁串行化同范围编辑，不阻塞目录/报表子记录的外键校验。
    session.exec(
        select(TenantBC)
        .where(
            col(TenantBC.tenant_id) == context.tenant_id, col(TenantBC.bc_id) == bc_id
        )
        .with_for_update(key_share=True)
    ).one()
    key = (context.tenant_id, bc_id, advertiser_id, campaign_id)
    row = session.get(BusinessMapping, key)
    previous = str(row.drama_id) if row else None
    if row is None:
        row = BusinessMapping(
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
            drama_id=drama_id,
            actor_id=context.actor_id,
        )
    row.drama_id, row.actor_id, row.updated_at = drama_id, context.actor_id, utcnow()
    session.add(row)
    bump_scopes(session, context.tenant_id, bc_id)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="business.mapping.update",
            target_id=f"{advertiser_id}:{campaign_id}",
            details={
                "bc_id": bc_id,
                "previous_drama_id": previous,
                "drama_id": str(drama_id),
            },
        )
    )
    session.flush()
    return row
