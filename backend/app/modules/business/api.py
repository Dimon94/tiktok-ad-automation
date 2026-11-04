"""经营API：请求只查投影或受理后台任务，不在页面请求中重算历史。"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.modules.accounts.models import AdvertiserAccount
from app.modules.business.configuration import (
    bump_scopes,
    save_mapping,
    save_rate,
    scope_accounts,
)
from app.modules.business.exports import check_export, create_export, export_chunks
from app.modules.business.models import (
    BusinessEvidence,
    BusinessMember,
    BusinessRate,
    BusinessRun,
    BusinessScope,
)
from app.modules.business.projection import request_rebuild
from app.modules.business.queries import query_dashboard
from app.modules.business.schemas import (
    DashboardFilter,
    DashboardPublic,
    EvidenceInput,
    EvidencePublic,
    ExportPublic,
    MappingInput,
    MemberInput,
    RateInput,
    RatePublic,
    RebuildInput,
)
from app.modules.providers.models import ProviderConnection, ProviderDrama
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}/business", tags=["business"])


@router.post("/query", response_model=DashboardPublic)
def dashboard(
    tenant_id: UUID, body: DashboardFilter, session: SessionDep, user: CurrentUser
) -> DashboardPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return query_dashboard(session, context=context, filters=body)


@router.post("/rebuild", status_code=202)
def rebuild(
    tenant_id: UUID, body: RebuildInput, session: SessionDep, user: CurrentUser
) -> dict[str, list[str]]:
    # 生成本地只读投影不等于修改广告，普通查询角色可初始化自身已授权范围。
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    runs = request_rebuild(session, context=context, **body.model_dump())
    session.commit()
    return {"run_ids": [str(r.id) for r in runs]}


@router.get("/options")
def options(
    tenant_id: UUID, bc_id: str, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    allowed = scope_accounts(session, context, bc_id)
    accounts = (
        session.exec(
            select(AdvertiserAccount).where(
                col(AdvertiserAccount.tenant_id) == tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(allowed),
            )
        ).all()
        if allowed
        else []
    )
    providers = session.exec(
        select(ProviderConnection)
        .where(
            col(ProviderConnection.tenant_id) == tenant_id,
            col(ProviderConnection.kind) != "other",
        )
        .order_by(col(ProviderConnection.display_name))
    ).all()
    cutoff = min(
        (
            datetime.now(UTC).astimezone(ZoneInfo(a.timezone)).date()
            - timedelta(days=1)
            for a in accounts
        ),
        default=datetime.now(UTC).date() - timedelta(days=1),
    )
    return {
        "accounts": [
            {
                "id": a.advertiser_id,
                "name": a.name or a.advertiser_id,
                "currency": a.currency,
                "timezone": a.timezone,
                "end_date": (
                    datetime.now(UTC).astimezone(ZoneInfo(a.timezone)).date()
                    - timedelta(days=1)
                ).isoformat(),
            }
            for a in accounts
        ],
        "providers": [{"id": str(p.id), "name": p.display_name} for p in providers],
        "end_date": cutoff.isoformat(),
    }


@router.get("/dramas")
def dramas(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    q: str = Query(default="", max_length=255),
    provider_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    statement = select(ProviderDrama).where(col(ProviderDrama.tenant_id) == tenant_id)
    if provider_id:
        statement = statement.where(col(ProviderDrama.connection_id) == provider_id)
    if q:
        statement = statement.where(
            or_(
                col(ProviderDrama.title).icontains(q, autoescape=True),
                col(ProviderDrama.external_drama_id).icontains(q, autoescape=True),
            )
        )
    return [
        {
            "id": str(d.id),
            "title": d.title,
            "external_id": d.external_drama_id,
            "provider_id": str(d.connection_id),
            "application_id": d.application_id,
        }
        for d in session.exec(
            statement.order_by(col(ProviderDrama.title), col(ProviderDrama.id)).limit(
                limit
            )
        ).all()
    ]


@router.get("/rates", response_model=list[RatePublic])
def rates(
    tenant_id: UUID, kind: str, scope_id: str, session: SessionDep, user: CurrentUser
) -> Sequence[BusinessRate]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    return session.exec(
        select(BusinessRate)
        .where(
            col(BusinessRate.tenant_id) == tenant_id,
            col(BusinessRate.kind) == kind,
            col(BusinessRate.scope_id) == scope_id,
        )
        .order_by(
            col(BusinessRate.effective_date).desc(), col(BusinessRate.revision).desc()
        )
    ).all()


@router.post("/rates", response_model=RatePublic)
def set_rate(
    tenant_id: UUID, body: RateInput, session: SessionDep, user: CurrentUser
) -> BusinessRate:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = save_rate(session, context=context, **body.model_dump())
    session.commit()
    return row


@router.put("/mapping")
def mapping(
    tenant_id: UUID, body: MappingInput, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = save_mapping(session, context=context, **body.model_dump())
    session.commit()
    return {"drama_id": str(row.drama_id)}


@router.get("/evidence", response_model=EvidencePublic)
def read_evidence(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: str,
    advertiser_id: str,
    campaign_id: str,
    stat_date: date,
) -> EvidencePublic:
    from app.modules.business.evidence import read_evidence as read

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    row = read(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        stat_date=stat_date,
    )
    return (
        EvidencePublic(bid=row.bid, eligible=row.eligible, source=row.source)
        if row
        else EvidencePublic()
    )


@router.put("/evidence")
def evidence(
    tenant_id: UUID, body: EvidenceInput, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    if body.advertiser_id not in scope_accounts(session, context, body.bc_id):
        raise HTTPException(403, "当前BC未授权账户")
    row = BusinessEvidence(**body.model_dump(), tenant_id=tenant_id, actor_id=user.id)
    session.merge(row)
    bump_scopes(session, tenant_id, body.bc_id)
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user.id,
            action="business.evidence.update",
            target_id=f"{body.advertiser_id}:{body.campaign_id}:{body.stat_date}",
            details={
                "bc_id": body.bc_id,
                "source": body.source,
                "bid": str(body.bid),
                "eligible": body.eligible,
            },
        )
    )
    session.commit()
    return {"saved": True}


@router.put("/member")
def member(
    tenant_id: UUID, body: MemberInput, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    if body.advertiser_id not in scope_accounts(session, context, body.bc_id):
        raise HTTPException(403, "当前BC未授权账户")
    if body.end_date is not None and body.end_date < body.start_date:
        raise HTTPException(422, "成员结束日期早于开始日期")
    row = BusinessMember(**body.model_dump(), tenant_id=tenant_id)
    session.merge(row)
    bump_scopes(session, tenant_id, body.bc_id)
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user.id,
            action="business.member.update",
            target_id=body.advertiser_id,
            details=body.model_dump(mode="json"),
        )
    )
    session.commit()
    return {"saved": True}


@router.get("/runs")
def runs(
    tenant_id: UUID, bc_id: str, session: SessionDep, user: CurrentUser
) -> list[dict[str, Any]]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    scope_accounts(session, context, bc_id)
    scope = session.get(BusinessScope, (tenant_id, bc_id))
    statement = select(BusinessRun).where(
        col(BusinessRun.tenant_id) == tenant_id, col(BusinessRun.bc_id) == bc_id
    )
    rows = session.exec(
        statement.order_by(col(BusinessRun.created_at).desc()).limit(20)
    ).all()
    return [
        {
            "id": str(r.id),
            "quarter": str(r.quarter),
            "end_date": str(r.end_date),
            "status": r.status,
            "phase": r.phase,
            "cursor": str(r.cursor),
            "rows": r.rows_count,
            "error": r.error,
            "configuration_changed": scope is not None
            and scope.revision != r.scope_revision,
        }
        for r in rows
    ]


@router.post("/exports", response_model=ExportPublic, status_code=202)
def export(
    tenant_id: UUID, body: DashboardFilter, session: SessionDep, user: CurrentUser
) -> ExportPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    row = create_export(session, context=context, filters=body)
    session.commit()
    return ExportPublic(id=row.id, status=row.status, expires_at=row.expires_at)


@router.get("/exports/{export_id}", response_model=ExportPublic)
def export_status(
    tenant_id: UUID, export_id: UUID, session: SessionDep, user: CurrentUser
) -> ExportPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    row = check_export(session, context=context, export_id=export_id)
    return ExportPublic(id=row.id, status=row.status, expires_at=row.expires_at)


@router.get("/exports/{export_id}/download")
def download(
    tenant_id: UUID, export_id: UUID, session: SessionDep, user: CurrentUser
) -> StreamingResponse:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return StreamingResponse(
        export_chunks(session, context=context, export_id=export_id),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="business-{export_id}.csv"',
        },
    )
