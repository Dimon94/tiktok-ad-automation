"""B3 本地只读报表 API。

这些端点只访问本地发布表和查询快照；普通列表/趋势请求不会隐式调用 TikTok。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal, cast
from typing import cast as typing_cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from redis import Redis, RedisError
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import AdvertiserAccount, TenantBC
from app.modules.ads.directory import DirectoryObjectPage, read_directory_objects
from app.modules.reporting.ad_materials import (
    AdMaterialsPublic,
    freeze_ad_material_selection,
    list_ad_materials,
)
from app.modules.reporting.balance_queries import latest_balance_observations
from app.modules.reporting.detail import AdDetailPublic, get_ad_detail
from app.modules.reporting.exports import (
    create_export,
    download_export,
    get_export,
    list_exports,
)
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.metric_catalog import (
    DERIVED_RATIOS,
    METRIC_CATALOG,
    NATIVE_CONTRACT,
    platform_ratio_contract,
    semantics_for,
    table_label,
)
from app.modules.reporting.page_cache import query_cached_ads
from app.modules.reporting.planner import (
    backfill_allowed,
    ensure_bc_plans,
    local_today,
    owned_accounts,
    plan_hot,
)
from app.modules.reporting.preferences import (
    delete_view,
    list_views,
    save_view,
    update_view,
)
from app.modules.reporting.query_reuse import QueryCancellation
from app.modules.reporting.schemas import (
    AdContributionScope,
    AdMaterialSelectionRequest,
    AdsQueryPage,
    ExportCreate,
    ExportPublic,
    FrozenSelection,
    MetricDefinitionPublic,
    ReportingFilter,
    SavedViewCreate,
    SavedViewPatch,
    SavedViewPublic,
    SelectionRequest,
    SyncAccountStatus,
    SyncHealthPublic,
    SyncPlanStatus,
    SyncRefreshPublic,
    SyncRefreshRequest,
    SyncStatusPublic,
    TrendLevel,
    TrendPublic,
)
from app.modules.reporting.selection import freeze_selection
from app.modules.reporting.store_models import (
    AdSyncBc,
    ReportSyncHealth,
    ReportSyncState,
)
from app.modules.reporting.trend import build_trend
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["ads_reporting"])
Dimension = Literal["account", "campaign", "adgroup", "ad", "material", "drama"]
CSV = Annotated[str | None, Query(max_length=4096)]


def _tuple(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _filters(
    *,
    dimension: Dimension,
    start_date: date,
    end_date: date,
    advertiser_id: str | None,
    ids: str | None,
    query: str | None,
    ad_types: str | None,
    operation_statuses: str | None,
    review_statuses: str | None,
    budget_modes: str | None,
    naming_status: str | None,
    min_spend: Decimal | None,
    max_spend: Decimal | None,
    min_d0_roas: Decimal | None,
    max_d0_roas: Decimal | None,
    min_target_roas: Decimal | None,
    max_target_roas: Decimal | None,
    created_from: date | None,
    created_to: date | None,
    sort_by: str,
    sort_direction: Literal["asc", "desc"],
    include_empty: bool,
    ad_contribution_ids: str | None = None,
) -> ReportingFilter:
    return ReportingFilter(
        dimension=dimension,
        start_date=start_date,
        end_date=end_date,
        advertiser_ids=(advertiser_id,) if advertiser_id else (),
        ids=_tuple(ids),
        contribution=AdContributionScope(kind="ad", ids=_tuple(ad_contribution_ids))
        if ad_contribution_ids
        else None,
        query=query,
        ad_types=_tuple(ad_types),
        operation_statuses=_tuple(operation_statuses),
        review_statuses=_tuple(review_statuses),
        budget_modes=_tuple(budget_modes),
        naming_status=naming_status,
        min_spend=min_spend,
        max_spend=max_spend,
        min_d0_roas=min_d0_roas,
        max_d0_roas=max_d0_roas,
        min_target_roas=min_target_roas,
        max_target_roas=max_target_roas,
        created_from=created_from,
        created_to=created_to,
        sort_by=sort_by,
        sort_direction=sort_direction,
        include_empty=include_empty,
    )


def _context(
    session: Session,
    user: CurrentUser,
    tenant_id: UUID,
    *,
    action: Literal["read", "reporting_write"] = "read",
) -> TenantContext:
    return require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action=action)


@router.get("/ads", response_model=AdsQueryPage, name="query_ads")
async def query_ads_route(
    request: Request,
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    dimension: Dimension = "campaign",
    start_date: date | None = None,
    end_date: date | None = None,
    advertiser_id: str | None = Query(default=None, max_length=128),
    ids: CSV = None,
    ad_contribution_ids: CSV = None,
    query: CSV = None,
    ad_types: CSV = None,
    operation_statuses: CSV = None,
    review_statuses: CSV = None,
    budget_modes: CSV = None,
    naming_status: str | None = Query(default=None, max_length=32),
    min_spend: Decimal | None = None,
    max_spend: Decimal | None = None,
    min_d0_roas: Decimal | None = None,
    max_d0_roas: Decimal | None = None,
    min_target_roas: Decimal | None = None,
    max_target_roas: Decimal | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    sort_by: str = Query(default="row_key", max_length=64),
    sort_direction: Literal["asc", "desc"] = "asc",
    include_empty: bool = False,
    snapshot_id: UUID | None = None,
    cursor: Annotated[str | None, Query(max_length=8192)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    force_refresh: bool = False,
    view: Literal["metrics", "evidence"] = "metrics",
    evidence_row_key: Annotated[str | None, Query(max_length=512)] = None,
    evidence_bucket: Annotated[str | None, Query(max_length=1024)] = None,
    evidence_component: Annotated[str | None, Query(max_length=128)] = None,
    evidence_cursor: Annotated[str | None, Query(max_length=8192)] = None,
) -> AdsQueryPage:
    # 默认日期必须按请求时刻计算；写在参数默认值里会固定为服务启动当天。
    today = date.today()
    filters = _filters(
        dimension=dimension,
        start_date=start_date or today,
        end_date=end_date or today,
        advertiser_id=advertiser_id,
        ids=ids,
        ad_contribution_ids=ad_contribution_ids,
        query=query,
        ad_types=ad_types,
        operation_statuses=operation_statuses,
        review_statuses=review_statuses,
        budget_modes=budget_modes,
        naming_status=naming_status,
        min_spend=min_spend,
        max_spend=max_spend,
        min_d0_roas=min_d0_roas,
        max_d0_roas=max_d0_roas,
        min_target_roas=min_target_roas,
        max_target_roas=max_target_roas,
        created_from=created_from,
        created_to=created_to,
        sort_by=sort_by,
        sort_direction=sort_direction,
        include_empty=include_empty,
    )
    cancellation = QueryCancellation()

    def execute_query() -> AdsQueryPage:
        context = _context(session, user, tenant_id)
        if view == "evidence":
            import json

            from pydantic import ValidationError

            from app.modules.reporting.frozen_evidence import read_snapshot_evidence
            from app.modules.reporting.schemas import EvidenceBucket

            try:
                bucket = (
                    EvidenceBucket.model_validate_json(evidence_bucket).model_dump()
                    if evidence_bucket
                    else None
                )
            except (ValidationError, json.JSONDecodeError) as error:
                raise HTTPException(422, "invalid_evidence_bucket") from error
            return read_snapshot_evidence(
                session,
                context=context,
                bc_id=bc_id,
                snapshot_id=snapshot_id,
                row_key=evidence_row_key,
                bucket=bucket,
                component=evidence_component,
                cursor=evidence_cursor,
                limit=limit,
            )
        result = query_cached_ads(
            session,
            context=context,
            bc_id=bc_id,
            filters=filters,
            snapshot_id=snapshot_id,
            cursor=cursor,
            limit=limit,
            cancellation=cancellation,
            force_refresh=force_refresh,
        )
        session.commit()
        return result

    task = asyncio.create_task(asyncio.to_thread(execute_query))
    try:
        while not task.done():
            if await request.is_disconnected():
                from app.core.db import engine

                await asyncio.to_thread(cancellation.cancel, engine)
                break
            await asyncio.wait({task}, timeout=0.1)
        try:
            result = await asyncio.shield(task)
        except Exception:
            if cancellation.event.is_set():
                raise HTTPException(499, detail="query_cancelled") from None
            raise
        if cancellation.event.is_set():
            raise HTTPException(499, detail="query_cancelled")
        return result
    except asyncio.CancelledError:
        # 保持Session依赖存活直到执行线程退出，不能在SQL仍运行时把连接还给池。
        from app.core.db import engine

        await asyncio.to_thread(cancellation.cancel, engine)
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


@router.get(
    "/ads/metric-catalog",
    response_model=tuple[MetricDefinitionPublic, ...],
    name="metric_catalog",
)
def metric_catalog_route(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> tuple[MetricDefinitionPublic, ...]:
    _context(session, user, tenant_id)
    return tuple(
        MetricDefinitionPublic(
            **asdict(item),
            source_kind="LOCAL_CALCULATION"
            if any(r[0] == item.key for r in DERIVED_RATIOS)
            else "PLATFORM_DIRECT",
            contract=semantics_for(item.key, NATIVE_CONTRACT),
            platform_observation=platform_ratio_contract(item.key),
            basic_label=table_label(item.basic_name_zh),
            material_label=table_label(item.material_name_zh)
            if item.material_name_zh
            else None,
        )
        for item in METRIC_CATALOG
    )


# 必须注册在 /ads/{kind}/{remote_id} 之前，避免静态路径被详情路由的参数段吞掉。
@router.get("/ads/trend", response_model=TrendPublic, name="ad_trend")
def ad_trend_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    level: TrendLevel,
    start_date: date,
    end_date: date,
    session: SessionDep,
    user: CurrentUser,
    granularity: Literal["day", "hour"] = "day",
    advertiser_ids: Annotated[str | None, Query(max_length=20000)] = None,
    ids: Annotated[str | None, Query(max_length=20000)] = None,
    snapshot_id: UUID | None = None,
) -> TrendPublic:
    context = _context(session, user, tenant_id)
    trend = build_trend(
        session,
        context=context,
        bc_id=bc_id,
        level=level,
        advertiser_ids=_tuple(advertiser_ids),
        ids=_tuple(ids),
        start=start_date,
        end=end_date,
        granularity=granularity,
        snapshot_id=snapshot_id,
    )
    # 小时趋势可能排入了按需拉取任务，需提交后才会被调度派发。
    session.commit()
    return trend


# 固定路径须在 /ads/{kind}/{remote_id} 之前注册，避免被通配路由先匹配。
@router.get(
    "/ads/ad/{remote_id}/materials",
    response_model=AdMaterialsPublic,
    name="ad_materials",
)
def ad_materials_route(
    tenant_id: UUID,
    remote_id: str,
    advertiser_id: Annotated[str, Query(min_length=1, max_length=128)],
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    start_date: date,
    end_date: date,
    session: SessionDep,
    user: CurrentUser,
) -> AdMaterialsPublic:
    context = _context(session, user, tenant_id)
    return list_ad_materials(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        ad_id=remote_id,
        start=start_date,
        end=end_date,
    )


@router.post(
    "/ads/ad/{remote_id}/material-selections",
    response_model=FrozenSelection,
    name="freeze_ad_material_selection",
)
def freeze_ad_material_selection_route(
    tenant_id: UUID,
    remote_id: str,
    body: AdMaterialSelectionRequest,
    advertiser_id: Annotated[str, Query(min_length=1, max_length=128)],
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> FrozenSelection:
    context = _context(session, user, tenant_id, action="reporting_write")
    result = freeze_ad_material_selection(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        ad_id=remote_id,
        ad_material_ids=body.ad_material_ids,
    )
    session.commit()
    return result


@router.get(
    "/ad-directory", response_model=DirectoryObjectPage, name="read_directory_objects"
)
def directory_objects_route(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    kind: Literal["campaign", "adgroup", "ad", "creative"],
    advertiser_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
    parent_kind: Literal["campaign", "adgroup", "ad"] | None = None,
    parent_remote_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    parent_advertiser_id: Annotated[
        str | None, Query(min_length=1, max_length=128)
    ] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DirectoryObjectPage:
    context = _context(session, user, tenant_id)
    if (parent_kind is None) != (parent_remote_id is None):
        raise HTTPException(422, detail="invalid_directory_scope")
    parent_account = parent_advertiser_id or (
        advertiser_ids[0] if len(advertiser_ids) == 1 else None
    )
    if parent_kind is not None and not parent_account:
        raise HTTPException(422, detail="invalid_directory_scope")
    parent = None
    if parent_kind:
        # 上述成对参数与账户范围校验已经排除了空值。
        assert parent_account is not None and parent_remote_id is not None
        parent = EntityRef(tenant_id, parent_account, parent_kind, parent_remote_id)
    try:
        return read_directory_objects(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_ids=tuple(advertiser_ids),
            kind=kind,
            parent=parent,
            cursor=cursor,
            limit=limit,
        )
    except DomainError as exc:
        if exc.code == "invalid_cursor" or exc.code == "invalid_directory_scope":
            raise HTTPException(400, detail=exc.code) from exc
        raise HTTPException(404, detail="ad_not_found") from exc


@router.get("/ads/{kind}/{remote_id}", response_model=AdDetailPublic, name="ad_detail")
def ad_detail_route(
    tenant_id: UUID,
    kind: Literal["campaign", "adgroup", "ad", "creative"],
    remote_id: str,
    advertiser_id: Annotated[str, Query(min_length=1, max_length=128)],
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
    snapshot_id: UUID | None = None,
) -> AdDetailPublic:
    context = _context(session, user, tenant_id)
    return get_ad_detail(
        session,
        context=context,
        bc_id=bc_id,
        ref=EntityRef(tenant_id, advertiser_id, kind, remote_id),
        snapshot_id=snapshot_id,
    )


@router.post(
    "/ad-selections", response_model=FrozenSelection, name="freeze_ad_selection"
)
def freeze_ad_selection_route(
    tenant_id: UUID,
    body: SelectionRequest,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> FrozenSelection:
    context = _context(session, user, tenant_id, action="reporting_write")
    result = freeze_selection(session, context=context, bc_id=bc_id, request=body)
    session.commit()
    return result


@router.post("/report-views", response_model=SavedViewPublic, name="create_report_view")
def create_report_view_route(
    tenant_id: UUID,
    body: SavedViewCreate,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> SavedViewPublic:
    context = _context(session, user, tenant_id, action="reporting_write")
    result = save_view(
        session,
        context=context,
        bc_id=bc_id,
        name=body.name,
        filters=body.filters,
        columns=body.columns,
    )
    session.commit()
    return result


@router.get(
    "/report-views",
    response_model=tuple[SavedViewPublic, ...],
    name="list_report_views",
)
def list_report_views_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> tuple[SavedViewPublic, ...]:
    context = _context(session, user, tenant_id)
    return list_views(session, context=context, bc_id=bc_id)


@router.patch(
    "/report-views/{view_id}",
    response_model=SavedViewPublic,
    name="update_report_view",
)
def update_report_view_route(
    tenant_id: UUID,
    view_id: UUID,
    body: SavedViewPatch,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> SavedViewPublic:
    context = _context(session, user, tenant_id, action="reporting_write")
    result = update_view(
        session,
        context=context,
        bc_id=bc_id,
        view_id=view_id,
        name=body.name,
        filters=body.filters,
        columns=body.columns,
    )
    session.commit()
    return result


@router.delete("/report-views/{view_id}", name="delete_report_view")
def delete_report_view_route(
    tenant_id: UUID,
    view_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> dict[str, bool]:
    context = _context(session, user, tenant_id, action="reporting_write")
    delete_view(session, context=context, bc_id=bc_id, view_id=view_id)
    session.commit()
    return {"deleted": True}


@router.post(
    "/report-exports", response_model=ExportPublic, name="create_report_export"
)
def create_report_export_route(
    tenant_id: UUID,
    body: ExportCreate,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> ExportPublic:
    context = _context(session, user, tenant_id, action="reporting_write")
    result = create_export(
        session,
        context=context,
        bc_id=bc_id,
        snapshot_id=body.snapshot_id,
        idempotency_key=body.idempotency_key,
    )
    session.commit()
    return result


@router.get(
    "/report-exports",
    response_model=tuple[ExportPublic, ...],
    name="list_report_exports",
)
def list_report_exports_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> tuple[ExportPublic, ...]:
    context = _context(session, user, tenant_id)
    return list_exports(session, context=context, bc_id=bc_id)


@router.get(
    "/report-exports/{export_id}", response_model=ExportPublic, name="get_report_export"
)
def get_report_export_route(
    tenant_id: UUID,
    export_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> ExportPublic:
    context = _context(session, user, tenant_id)
    row = get_export(session, context=context, bc_id=bc_id, export_id=export_id)
    return ExportPublic(
        id=row.id,
        status=cast(
            Literal["QUEUED", "RUNNING", "COMPLETE", "FAILED", "EXPIRED"], row.status
        ),
        coverage=dict(row.coverage),
        expires_at=row.expires_at,
    )


@router.get("/report-exports/{export_id}/download", name="download_report_export")
def download_report_export_route(
    tenant_id: UUID,
    export_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> Response:
    context = _context(session, user, tenant_id)
    content = download_export(
        session, context=context, bc_id=bc_id, export_id=export_id
    )
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="report-{export_id}.csv"'
        },
    )


def _sync_scope(session: Session, context: TenantContext, bc_id: str) -> set[str]:
    bc = session.get(TenantBC, (context.tenant_id, bc_id))
    if bc is None or bc.ownership_conflict:
        raise HTTPException(404, detail="query_scope_not_found")
    return {
        grant.advertiser_id
        for grant in authorized_grants(session, context=context, bc_id=bc_id)
    }


@router.post(
    "/ad-sync-refresh", response_model=SyncRefreshPublic, name="request_ad_sync_refresh"
)
def request_ad_sync_refresh_route(
    tenant_id: UUID,
    body: SyncRefreshRequest,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> SyncRefreshPublic:
    context = _context(session, user, tenant_id, action="reporting_write")
    allowed = _sync_scope(session, context, bc_id)
    if not settings.ADS_SYNC_ENABLED:
        return SyncRefreshPublic(status="SYNC_DISABLED")
    if not set(body.advertiser_ids) <= allowed:
        raise HTTPException(403, detail="report_scope_forbidden")
    now = datetime.now(UTC)
    # 用户请求只建立当前租户的计划，不能顺带改动别的租户后台状态。
    ensure_bc_plans(session, now=now, tenant_id=tenant_id)
    plan = session.get(AdSyncBc, (tenant_id, bc_id))
    if plan is None or not plan.enabled:
        session.commit()
        return SyncRefreshPublic(status="API_BINDING_REQUIRED")
    accounts = sorted(
        set(body.advertiser_ids or owned_accounts(session, plan)) & allowed
    )
    count = plan_hot(session, plan, active=accounts, now=now, tier="manual")
    session.commit()
    return SyncRefreshPublic(status="QUEUED", queued=count, accounts=len(accounts))


# 按账户找出广告日报表连续“已完成”的日期段：降序编号后 日期+序号 在连续段内恒定。
_COMPLETE_RUNS_SQL = text(
    """
    SELECT advertiser_id, MIN(stat_date) AS run_start, MAX(stat_date) AS run_end
    FROM (
        SELECT advertiser_id, stat_date,
               stat_date + (ROW_NUMBER() OVER (
                   PARTITION BY advertiser_id ORDER BY stat_date DESC
               ))::int AS grp
        FROM report_sync_state
        WHERE tenant_id = :tenant_id
          AND report_kind = 'ad_day'
          AND status = 'COMPLETE'
          AND advertiser_id IN :advertiser_ids
    ) runs
    GROUP BY advertiser_id, grp
    """
).bindparams(bindparam("advertiser_ids", expanding=True))


def complete_data_start(
    session: Session,
    tenant_id: UUID,
    accounts: Sequence[AdvertiserAccount],
    now: datetime,
) -> date | None:
    """完整数据起始日：从这一天到各账户本地昨天，所有账户每天都已同步完成。

    今天仍在热刷新，由“数据更新到”表达，不参与判断。开户前的日期不会有报表，
    不能算作缺口：账户从开户起就连续完整时不限制起始日；开户时间未知的账户只能
    以自身连续段起点为准。任一账户昨天都不完整时没有完整区间，返回 None。
    """
    if not accounts:
        return None
    runs: dict[str, list[tuple[date, date]]] = {}
    for row in session.connection().execute(
        _COMPLETE_RUNS_SQL,
        {
            "tenant_id": tenant_id,
            "advertiser_ids": [account.advertiser_id for account in accounts],
        },
    ):
        runs.setdefault(row.advertiser_id, []).append((row.run_start, row.run_end))
    bounds: list[date] = []
    openings: list[date] = []
    for account in accounts:
        timezone = account.timezone or "UTC"
        yesterday = local_today(timezone, now) - timedelta(days=1)
        opened = (
            account.remote_created_at.astimezone(ZoneInfo(timezone)).date()
            if account.remote_created_at
            else None
        )
        if opened is not None:
            if opened > yesterday:
                continue  # 今天才开户，还没有可判断的完整日期
            openings.append(opened)
        run_start = next(
            (
                start
                for start, end in runs.get(account.advertiser_id, [])
                if start <= yesterday <= end
            ),
            None,
        )
        if run_start is None:
            return None
        if opened is None or run_start > opened:
            bounds.append(run_start)
    if bounds:
        return max(bounds)
    return min(openings) if openings else None


@router.get(
    "/ad-sync-status", response_model=SyncStatusPublic, name="get_ad_sync_status"
)
def get_ad_sync_status_route(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    start_date: date,
    end_date: date,
) -> SyncStatusPublic:
    context = _context(session, user, tenant_id)
    allowed = _sync_scope(session, context, bc_id)
    if start_date > end_date:
        raise HTTPException(422, detail="invalid_reporting_filter")
    plan = session.get(AdSyncBc, (tenant_id, bc_id))
    accounts = session.exec(
        select(AdvertiserAccount)
        .where(
            col(AdvertiserAccount.tenant_id) == tenant_id,
            col(AdvertiserAccount.advertiser_id).in_(allowed),
        )
        .order_by(col(AdvertiserAccount.advertiser_id))
    ).all()
    states = session.exec(
        select(ReportSyncState).where(
            col(ReportSyncState.tenant_id) == tenant_id,
            col(ReportSyncState.advertiser_id).in_(allowed),
            col(ReportSyncState.stat_date) >= start_date,
            col(ReportSyncState.stat_date) <= end_date,
        )
    ).all()
    from app.modules.reporting.levels import LEVELS
    from app.modules.reporting.schemas import LevelSyncStatus

    level_index = {(r.advertiser_id, r.report_kind, r.stat_date): r for r in states}
    by_key = {
        (r.advertiser_id, r.stat_date): r for r in states if r.report_kind == "ad_day"
    }
    balances = latest_balance_observations(
        session, tenant_id=tenant_id, advertisers=allowed
    )
    # 余额按独立观测版本刷新，不能继续展示旧快照中的金额。
    balance_version = [(b.advertiser_id, str(b.id)) for b in balances]
    # 启停/预算修改后先回读目录，可能尚无新的日报。目录发布必须独立推进版本，
    # 否则报表页面看不到已回读的最新开关。限定当前租户、BC 及可见账户，未发布不计入。
    # 每账户只读发布索引的首行；不能为每次轮询扫描已归档前的全部运行历史。
    # 仍以运行账本为准：空目录发布也推进版本，不能用当前 ad_object 代替。
    directory_version = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            text("""
        SELECT max(latest.published_version)
        FROM unnest(CAST(:advertisers AS text[])) AS a(advertiser_id)
        CROSS JOIN LATERAL (
          SELECT r.published_version FROM ad_directory_run r
          WHERE r.tenant_id=:tenant_id AND r.bc_id=:bc_id
            AND r.advertiser_id=a.advertiser_id AND r.status='COMPLETE'
            AND r.published_version IS NOT NULL
          ORDER BY r.published_version DESC LIMIT 1
        ) latest
        """),
            {"tenant_id": tenant_id, "bc_id": bc_id, "advertisers": list(allowed)},
        )
        .scalar_one()
    )
    from app.modules.reporting.frozen_result import RESULT_CONTRACT

    # 源证据/派生修订也能改变结果；金额相同不能吞掉新数据提示。
    bucket_versions = (
        session.connection()
        .execute(
            text("""
        SELECT advertiser_id,kind,bucket,source_revision,derived_revision
        FROM read_bucket_state WHERE tenant_id=:tenant AND advertiser_id=ANY(:advs)
        AND (bucket BETWEEN :start AND :end OR bucket=DATE '1970-01-01')
        ORDER BY advertiser_id,kind,bucket
    """),
            {
                "tenant": tenant_id,
                "advs": list(allowed),
                "start": start_date,
                "end": end_date,
            },
        )
        .all()
    )
    # 素材发布与失败覆盖也推进版本，页面不能只看广告日的最后成功时间。
    version = sha256(
        repr(
            sorted(
                (
                    r.advertiser_id,
                    r.report_kind,
                    str(r.stat_date),
                    r.status,
                    str(r.last_success_at),
                    r.publication_version,
                    str(r.observed_at),
                    r.active_contract,
                    r.publication_digest,
                    str(r.updated_at),
                )
                for r in states
            )
            + balance_version
            + [("directory", directory_version)]
            + [tuple(r) for r in bucket_versions]
            + [("result_contract", RESULT_CONTRACT)]
        ).encode()
    ).hexdigest()
    days = [
        start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)
    ]
    result = []
    now = datetime.now(UTC)
    for account in accounts:
        today = local_today(account.timezone or "UTC", now)
        current_days = [day for day in days if day <= today]
        current = [by_key.get((account.advertiser_id, day)) for day in current_days]
        # 今日时效与历史完整性分开：历史成功时间不能拉低今日水位，
        # 今日缺失也不能借历史成功伪装成新鲜；日期缺口仍逐日返回。
        freshness_rows = (
            [by_key.get((account.advertiser_id, today))]
            if today in current_days
            else current
        )
        successes = [
            r.last_success_at
            for r in freshness_rows
            if r is not None and r.last_success_at is not None
        ]
        result.append(
            SyncAccountStatus(
                advertiser_id=account.advertiser_id,
                timezone=account.timezone,
                local_today=today,
                levels=tuple(
                    LevelSyncStatus(
                        level=contract.level,
                        stat_date=day,
                        status=state.status if state else "MISSING",
                        last_success_at=state.last_success_at if state else None,
                        last_attempt_at=state.updated_at if state else None,
                        observed_at=state.observed_at if state else None,
                        publication_version=state.publication_version if state else 0,
                        contract_revision=state.active_contract if state else None,
                        reason=state.error_code if state else "NO_OBSERVATION",
                    )
                    for kind, contract in LEVELS.items()
                    for day in current_days
                    for state in [level_index.get((account.advertiser_id, kind, day))]
                ),
                last_success_at=min(successes) if successes else None,
                incomplete_dates=tuple(
                    day
                    for day, row in zip(current_days, current, strict=True)
                    if row is None or row.status != "COMPLETE"
                ),
                failed_dates=tuple(
                    day
                    for day, row in zip(current_days, current, strict=True)
                    if row is not None and row.status == "FAILED"
                ),
            )
        )
    enabled = settings.ADS_SYNC_ENABLED and plan is not None and plan.enabled
    reason = (
        "sync_disabled"
        if not settings.ADS_SYNC_ENABLED
        else (plan.disabled_reason if plan is not None else "api_binding_required")
    )
    health_row = session.get(ReportSyncHealth, (tenant_id, bc_id))
    health = SyncHealthPublic(
        status="DISABLED" if not enabled else "DEGRADED",
        issues=("MONITOR_PENDING",) if enabled else (),
    )
    if enabled and health_row is not None:
        health = SyncHealthPublic(
            status="HEALTHY" if health_row.status == "HEALTHY" else "DEGRADED",
            observed_at=health_row.observed_at,
            issues=tuple(health_row.issues),
            **health_row.metrics,
        )
        from app.modules.reporting.health import _health_scope, coverage_issues
        from app.modules.reporting.planner import owned_accounts

        # 分钟水位不能替旧目录/关联覆盖续期；授权、规则和当地日变化也要立即可见。
        scope = _health_scope(session, plan, owned_accounts(session, plan), now=now)
        coverage = coverage_issues(health_row.metrics, now=now, scope=scope)
        if coverage:
            health.status = "DEGRADED"
            health.issues = tuple(dict.fromkeys((*health.issues, *coverage)))
        if health_row.observed_at < now - timedelta(minutes=3):
            health.status = "DEGRADED"
            health.issues = (*health.issues, "MONITOR_STALE")
    return SyncStatusPublic(
        health=health,
        accounts=tuple(result),
        complete_from=complete_data_start(
            session, tenant_id, accounts, datetime.now(UTC)
        ),
        publication_version=version,
        plan=SyncPlanStatus(
            enabled=enabled,
            disabled_reason=reason,
            next_hot_at=plan.next_hot_at if plan else None,
            slowed=_sync_slowed(),
        ),
    )


def _sync_slowed() -> bool | None:
    try:
        with Redis.from_url(
            settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1
        ) as redis:
            return not backfill_allowed(redis, datetime.now(UTC))
    except RedisError:
        # 额度状态不可读不能伪装成正常，也不阻断已发布数据的本地查询。
        return None
