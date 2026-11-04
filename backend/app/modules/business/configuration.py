"""商业费率与归属：按当前权限核验，修改推进计算围栏并留下审计。"""

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.models import TenantBC
from app.modules.business.models import (
    BusinessMapping,
    BusinessRate,
    BusinessScope,
    utcnow,
)
from app.modules.providers.models import ProviderConnection, ProviderDrama
from app.modules.reporting.filters import authorized_grants
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant


def scope_accounts(session: Session, context: TenantContext, bc_id: str) -> list[str]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
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
    session.execute(
        statement.values(
            revision=col(BusinessScope.revision) + 1,
            last_daily=None,
            next_check_at=utcnow(),
        )
    )


def rate_on(
    session: Session, tenant_id: UUID, kind: str, scope_id: str, day: date
) -> Decimal:
    row = session.exec(
        select(BusinessRate)
        .where(
            col(BusinessRate.tenant_id) == tenant_id,
            col(BusinessRate.kind) == kind,
            col(BusinessRate.scope_id) == scope_id,
            col(BusinessRate.effective_date) <= day,
        )
        .order_by(
            col(BusinessRate.effective_date).desc(), col(BusinessRate.revision).desc()
        )
        .limit(1)
    ).first()
    return row.rate if row else Decimal(0)


def save_rate(
    session: Session,
    *,
    context: TenantContext,
    kind: str,
    scope_id: str,
    effective_date: date,
    rate: Decimal,
) -> BusinessRate:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if not rate.is_finite() or not 0 <= rate <= 1:
        raise ValueError("比例必须在0到1之间")
    parent: ProviderConnection | TenantBC | None
    provider_id = None
    bc_id = None
    if kind == "provider":
        try:
            provider_id = UUID(scope_id)
        except ValueError:
            raise DomainError("provider_request_invalid", "版权方编号无效") from None
        parent = session.exec(
            select(ProviderConnection)
            .where(
                col(ProviderConnection.tenant_id) == context.tenant_id,
                col(ProviderConnection.id) == provider_id,
            )
            .with_for_update()
        ).first()
    elif kind == "rebate":
        bc_id = scope_id
        parent = session.exec(
            select(TenantBC)
            .where(
                col(TenantBC.tenant_id) == context.tenant_id,
                col(TenantBC.bc_id) == bc_id,
            )
            .with_for_update()
        ).first()
    else:
        raise ValueError("未知费率类别")
    if parent is None:
        raise DomainError("resource_not_found", "当前租户配置对象不存在")
    revision = (
        session.exec(
            select(func.coalesce(func.max(col(BusinessRate.revision)), 0)).where(
                col(BusinessRate.tenant_id) == context.tenant_id,
                col(BusinessRate.kind) == kind,
                col(BusinessRate.scope_id) == scope_id,
            )
        ).one()
        + 1
    )
    row = BusinessRate(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        kind=kind,
        scope_id=scope_id,
        provider_id=provider_id,
        bc_id=bc_id,
        effective_date=effective_date,
        rate=rate,
        revision=revision,
    )
    session.add(row)
    bump_scopes(session, context.tenant_id, bc_id)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="business.rate.update",
            target_id=str(row.id),
            details={
                "kind": kind,
                "scope_id": scope_id,
                "effective_date": effective_date.isoformat(),
                "rate": str(rate),
                "revision": revision,
            },
        )
    )
    session.flush()
    return row


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
    # scope行串行化同范围编辑；初始化前由账户授权行与唯一键防止跨范围写入。
    session.exec(
        select(TenantBC)
        .where(
            col(TenantBC.tenant_id) == context.tenant_id, col(TenantBC.bc_id) == bc_id
        )
        .with_for_update()
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
