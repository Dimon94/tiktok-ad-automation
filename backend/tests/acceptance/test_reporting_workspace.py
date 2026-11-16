"""B6 阶段验收：工作台跨租户/BC 一致性与本地查询容量。

所有数据均在 PostgreSQL 测试库内合成；本文件不建立 TikTok/MCP 客户端，因而
可以把“本地读请求没有外部调用”作为明确断言。容量场景也只测聚合查询本身，
不把合成数据量写成真实业务规模。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.integrations.tiktok.contracts.ads import EntityRef
from app.models import User
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.exports import (
    create_export,
    download_export,
    generate_export,
)
from app.modules.reporting.publish import (
    AccountMeta,
    publish_ad_day,
    publish_creative_day,
)
from app.modules.reporting.queries import query_ads
from app.modules.reporting.schemas import (
    AdsQueryPage,
    Dimension,
    ReportingFilter,
    SelectionRequest,
)
from app.modules.reporting.selection import freeze_selection
from app.modules.tenants.models import Tenant, TenantMembership
from tests.modules.reporting.sync_conftest import ad_row


@dataclass(frozen=True)
class _Scope:
    context: TenantContext
    bc_id: str
    connection_id: UUID
    advertiser_id: str


@dataclass(frozen=True)
class _WorkspaceCase:
    primary: _Scope
    other: _Scope
    campaign_refs: tuple[EntityRef, ...]


def _scope(session: Session, *, label: str, bc_id: str) -> _Scope:
    tenant_id, actor_id = uuid4(), uuid4()
    # 先提交被复合外键引用的租户/用户，再提交 membership 与 BC/账户目录。
    # 显式分段避免真实 PostgreSQL flush 时账户外键先于 tenant 插入。
    session.add_all(
        [
            User(
                id=actor_id,
                username=f"b6-{label}-{actor_id.hex[:10]}",
                hashed_password="test-only",
                is_active=True,
            ),
            Tenant(id=tenant_id, name=f"B6 tenant {label}"),
        ]
    )
    session.flush()
    session.add(
        TenantMembership(
            tenant_id=tenant_id,
            user_id=actor_id,
            role="operator",
            active=True,
        )
    )
    connection = TikTokConnection(
        tenant_id=tenant_id,
        status="ACTIVE",
        kind="OFFICIAL_API",
        adapter_contract_revision="official-api-v1",
    )
    session.add_all(
        [
            connection,
            TenantBC(tenant_id=tenant_id, bc_id=bc_id, name=f"B6 {bc_id}"),
            AdvertiserAccount(
                tenant_id=tenant_id,
                advertiser_id=f"account-{label}",
                name=f"Account {label}",
                currency="USD",
                timezone="UTC",
                remote_status="ENABLE",
            ),
        ]
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=tenant_id,
            bc_id=bc_id,
            advertiser_id=f"account-{label}",
            connection_id=connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            permission_state="VERIFIED",
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    return _Scope(
        context=TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator"),
        bc_id=bc_id,
        connection_id=connection.id,
        advertiser_id=f"account-{label}",
    )


def _campaign(
    session: Session,
    *,
    scope: _Scope,
    index: int,
    provider: str,
    title: str,
    spend: Decimal,
    revenue: Decimal,
    status: str = "ENABLE",
) -> EntityRef:
    campaign_id = f"campaign-{index}"
    ref = EntityRef(
        scope.context.tenant_id, scope.advertiser_id, "campaign", campaign_id
    )
    observed_at = datetime(2026, 9, 30, 1, tzinfo=UTC)
    session.add(
        AdObject(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            kind="campaign",
            remote_id=campaign_id,
            ad_type="REGULAR",
            name=f"{provider}-{title}-素材{index}",
            operation_status=status,
            review_status="APPROVED",
            delivery_status="PAUSED" if status == "PAUSED" else "ACTIVE",
            observed_at=observed_at,
            published_version=1,
            source_connection_id=scope.connection_id,
            source_channel="OFFICIAL_API",
        )
    )
    session.add(
        CampaignNameProjection(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            campaign_remote_id=campaign_id,
            name_revision=1,
            raw_name=f"{provider}-{title}-素材{index}",
            provider_label=provider,
            drama_name=title,
            status="VALID",
            parser_revision=1,
            grouping_revision=1,
            observed_at=observed_at,
        )
    )
    # 多次添加系列时累计该账户的合成原始行，再走整份发布。
    key = (scope.context.tenant_id, scope.advertiser_id)
    rows = session.info.setdefault("workspace_report_rows", {}).setdefault(key, [])
    day = date(2026, 9, 30)
    rows.append(
        ad_row(
            f"ad-{index}",
            day,
            campaign=campaign_id,
            adv=scope.advertiser_id,
            spend=str(spend),
            d0=str(revenue),
            total=str(revenue),
        )
    )
    session.flush()
    publish_ad_day(
        session,
        tenant_id=scope.context.tenant_id,
        job_id=uuid4(),
        accounts={scope.advertiser_id: AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=rows,
    )
    return ref


def _external_material(session: Session, *, scope: _Scope, ref: EntityRef) -> None:
    """外部素材没有本地 material_file，仍保留精确广告内使用身份。"""
    ad_id = "ad-external-1"
    session.add(
        AdObject(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            kind="ad",
            remote_id=ad_id,
            parent_kind="campaign",
            parent_remote_id=ref.remote_id,
            ad_type="REGULAR",
            name="外部素材广告",
            operation_status="ENABLE",
            review_status="APPROVED",
            observed_at=datetime.now(UTC),
            published_version=1,
            source_connection_id=scope.connection_id,
            source_channel="OFFICIAL_API",
        )
    )
    session.add(
        AdMaterialReference(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            ad_remote_id=ad_id,
            platform_material_id="vid-external-1",
            main_material_id="vid-external-1",
            main_material_type="VIDEO",
            ad_material_id="ad-material-1",
            material_type="VIDEO",
            name="外部视频.mp4",
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    day = date(2026, 9, 30)
    publish_creative_day(
        session,
        tenant_id=scope.context.tenant_id,
        job_id=uuid4(),
        accounts={scope.advertiser_id: AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[
            {
                "dimensions": {
                    "advertiser_id": scope.advertiser_id,
                    "ad_id": "creative-external-1",
                    "stat_time_day": str(day),
                },
                "metrics": {
                    "spend": "7",
                    "campaign_id": ref.remote_id,
                    "adgroup_id": "external-group",
                },
            }
        ],
    )
    from app.modules.ads.creatives import upsert_creatives

    upsert_creatives(
        session,
        tenant_id=scope.context.tenant_id,
        advertiser_id=scope.advertiser_id,
        rows=[
            {
                "creative_id": "creative-external-1",
                "ad_remote_id": ad_id,
                "ad_material_id": "ad-material-1",
                "assets": [
                    {"material_type": "VIDEO", "platform_material_id": "vid-external-1"}
                ],
            }
        ],
        observed_at=datetime.now(UTC),
        published_version=1,
    )
    session.flush()


@pytest.fixture
def workspace_case(session: Session) -> _WorkspaceCase:
    primary = _scope(session, label="primary", bc_id="bc-b6-primary")
    other = _scope(session, label="other", bc_id="bc-b6-other")
    providers = ("版权方甲", "版权方乙", "版权方丙")
    refs = tuple(
        _campaign(
            session,
            scope=primary,
            index=index,
            provider=provider,
            title="同名剧",
            spend=Decimal(index * 10),
            revenue=Decimal(index * 20),
            status="PAUSED" if index == 2 else ("DELETED" if index == 3 else "ENABLE"),
        )
        for index, provider in enumerate(providers, start=1)
    )
    _external_material(session, scope=primary, ref=refs[0])
    _campaign(
        session,
        scope=other,
        index=1,
        provider="其他版权方",
        title="同名剧",
        spend=Decimal("999"),
        revenue=Decimal("999"),
    )
    session.flush()
    return _WorkspaceCase(primary, other, refs)


def _filters(dimension: Dimension = "campaign") -> ReportingFilter:
    return ReportingFilter(
        dimension=dimension,
        start_date=date(2026, 9, 30),
        end_date=date(2026, 9, 30),
    )


def _summary_buckets(page: AdsQueryPage) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], page.summary["buckets"])


def _bucket_values(bucket: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], bucket["values"])


def test_workspace_fastapi_contract_has_no_external_write_routes() -> None:
    """B5 的本地 reporting 路由仍是唯一工作台合同，未新增平台写端点。"""
    from app.main import app

    paths = app.openapi()["paths"]
    expected_operations = {
        (
            "GET",
            "/api/tenants/{tenant_id}/ad-directory",
        ): "ads_reporting-read_directory_objects",
        ("GET", "/api/tenants/{tenant_id}/ads"): "ads_reporting-query_ads",
        (
            "GET",
            "/api/tenants/{tenant_id}/ads/metric-catalog",
        ): "ads_reporting-metric_catalog",
        (
            "GET",
            "/api/tenants/{tenant_id}/ads/trend",
        ): "ads_reporting-ad_trend",
        (
            "GET",
            "/api/tenants/{tenant_id}/ads/{kind}/{remote_id}",
        ): "ads_reporting-ad_detail",
        (
            "POST",
            "/api/tenants/{tenant_id}/ad-selections",
        ): "ads_reporting-freeze_ad_selection",
        (
            "GET",
            "/api/tenants/{tenant_id}/report-views",
        ): "ads_reporting-list_report_views",
        (
            "POST",
            "/api/tenants/{tenant_id}/report-views",
        ): "ads_reporting-create_report_view",
        (
            "PATCH",
            "/api/tenants/{tenant_id}/report-views/{view_id}",
        ): "ads_reporting-update_report_view",
        (
            "DELETE",
            "/api/tenants/{tenant_id}/report-views/{view_id}",
        ): "ads_reporting-delete_report_view",
        (
            "GET",
            "/api/tenants/{tenant_id}/report-exports",
        ): "ads_reporting-list_report_exports",
        (
            "POST",
            "/api/tenants/{tenant_id}/report-exports",
        ): "ads_reporting-create_report_export",
        (
            "GET",
            "/api/tenants/{tenant_id}/report-exports/{export_id}",
        ): "ads_reporting-get_report_export",
        (
            "GET",
            "/api/tenants/{tenant_id}/report-exports/{export_id}/download",
        ): "ads_reporting-download_report_export",
    }
    expected_operations.update(
        {
            (
                "GET",
                "/api/tenants/{tenant_id}/ads/ad/{remote_id}/materials",
            ): "ads_reporting-ad_materials",
            (
                "POST",
                "/api/tenants/{tenant_id}/ads/ad/{remote_id}/material-selections",
            ): "ads_reporting-freeze_ad_material_selection",
            (
                "POST",
                "/api/tenants/{tenant_id}/ad-sync-refresh",
            ): "ads_reporting-request_ad_sync_refresh",
            (
                "GET",
                "/api/tenants/{tenant_id}/ad-sync-status",
            ): "ads_reporting-get_ad_sync_status",
        }
    )
    actual_operations = {
        (method.upper(), path): operation["operationId"]
        for path, methods in paths.items()
        for method, operation in methods.items()
        if operation.get("tags") == ["ads_reporting"]
    }
    # 该稳定映射就是生成客户端的静态输入：路径、HTTP method、operationId
    # 任一漂移都会使客户端合同与服务端不一致。
    assert actual_operations == expected_operations


def test_workspace_http_query_contract(
    workspace_case: _WorkspaceCase, session: Session, client, monkeypatch
) -> None:
    """通过真实 FastAPI/TestClient 读取已发布本地报表，禁止隐式平台请求。"""
    from app.api.deps import get_current_user
    from app.integrations.tiktok import gateway
    from app.main import app

    provider_calls: list[str] = []

    def fail_if_provider_called(*_args: object, **_kwargs: object) -> None:
        provider_calls.append("tiktok")
        raise AssertionError("reporting GET must not open a TikTok gateway")

    monkeypatch.setattr(gateway, "open_tiktok_gateway", fail_if_provider_called)
    user = session.get(User, workspace_case.primary.context.actor_id)
    assert user is not None
    # B3 的生产路由提交快照；在这个 fixture 中把 commit 降为 flush，
    # 让根 conftest 的 PostgreSQL 外层事务仍能回滚合成数据。
    client_commit = session.commit
    session.commit = cast(Any, lambda: session.flush())
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = client.get(
            f"/api/tenants/{workspace_case.primary.context.tenant_id}/ads",
            params={
                "bc_id": workspace_case.primary.bc_id,
                "dimension": "campaign",
                "start_date": "2026-09-30",
                "end_date": "2026-09-30",
                "limit": 2,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 3
        assert len(payload["items"]) == 2
        assert payload["next_cursor"]
        assert payload["snapshot"]["filters"]["dimension"] == "campaign"
        snapshot_id = payload["snapshot"]["snapshot_id"]
        second_page = client.get(
            f"/api/tenants/{workspace_case.primary.context.tenant_id}/ads",
            params={
                "bc_id": workspace_case.primary.bc_id,
                "dimension": "campaign",
                "start_date": "2026-09-30",
                "end_date": "2026-09-30",
                "limit": 2,
                "snapshot_id": snapshot_id,
                "cursor": payload["next_cursor"],
            },
        )
        assert second_page.status_code == 200, second_page.text
        assert len(second_page.json()["items"]) == 1
        trend = client.get(
            f"/api/tenants/{workspace_case.primary.context.tenant_id}/reports/trend",
            params={
                "bc_id": workspace_case.primary.bc_id,
                "dimension": "campaign",
                "start_date": "2026-09-30",
                "end_date": "2026-09-30",
                "snapshot_id": snapshot_id,
            },
        )
        assert trend.status_code == 404, trend.text
        item = payload["items"][0]
        detail = client.get(
            f"/api/tenants/{workspace_case.primary.context.tenant_id}/ads/campaign/{item['display']['remote_id']}",
            params={
                "advertiser_id": workspace_case.primary.advertiser_id,
                "bc_id": workspace_case.primary.bc_id,
                "snapshot_id": snapshot_id,
            },
        )
        assert detail.status_code == 200, detail.text
        assert {"ref", "name", "statuses"} <= set(detail.json())
        wrong_bc = client.get(
            f"/api/tenants/{workspace_case.primary.context.tenant_id}/ads",
            params={
                "bc_id": workspace_case.other.bc_id,
                "dimension": "campaign",
                "start_date": "2026-09-30",
                "end_date": "2026-09-30",
            },
        )
        # 未知/未授权 BC 在创建快照前被 admission 隐藏为 404，避免无效 FK/500。
        assert wrong_bc.status_code == 404, wrong_bc.text
        foreign_snapshot = client.get(
            f"/api/tenants/{workspace_case.primary.context.tenant_id}/ads",
            params={
                "bc_id": workspace_case.other.bc_id,
                "dimension": "campaign",
                "start_date": "2026-09-30",
                "end_date": "2026-09-30",
                "snapshot_id": snapshot_id,
            },
        )
        assert foreign_snapshot.status_code == 404, foreign_snapshot.text
        assert provider_calls == []
    finally:
        session.commit = cast(Any, client_commit)
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


def test_workspace_snapshot_contract(
    workspace_case: _WorkspaceCase, session: Session
) -> None:
    """列表、汇总、导出、全选共享同一租户/BC 冻结结果。"""
    case = workspace_case
    filters = _filters()
    page = query_ads(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        filters=filters,
        limit=2,
    )
    assert page.total == 3
    assert len(page.items) == 2
    assert {row.display["operation_status"] for row in page.items} <= {
        "ENABLE",
        "PAUSED",
        "DELETED",
    }
    assert {row.display["provider"] for row in page.items} <= {
        "版权方甲",
        "版权方乙",
        "版权方丙",
    }
    assert page.next_cursor is not None
    next_page = query_ads(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        filters=filters,
        snapshot_id=page.snapshot.snapshot_id,
        cursor=page.next_cursor,
        limit=2,
    )
    assert len(next_page.items) == 1
    assert {row.display["provider"] for row in page.items + next_page.items} == {
        "版权方甲",
        "版权方乙",
        "版权方丙",
    }
    assert next_page.summary == page.summary
    drama_page = query_ads(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        filters=_filters("drama"),
        limit=100,
    )
    # 剧只按系列名第二段归组：三个版权方的“同名剧”合并为一行，金额合计不变。
    assert drama_page.total == 1
    assert sum(
        Decimal(str(_bucket_values(bucket)["spend"]))
        for bucket in _summary_buckets(drama_page)
    ) == Decimal("60")
    bucket = _summary_buckets(page)[0]
    values = _bucket_values(bucket)
    assert Decimal(str(values["spend"])) == Decimal("60")
    assert Decimal(str(values["revenue_d0"])) == Decimal("120")
    assert Decimal(str(values["roas_d0"])) == Decimal("2")

    # 快照发布后新增一个目录成员；分页、全选和导出均只能消费已冻结的三行。
    session.add(
        AdObject(
            tenant_id=case.primary.context.tenant_id,
            advertiser_id=case.primary.advertiser_id,
            kind="campaign",
            remote_id="campaign-added-after-snapshot",
            ad_type="REGULAR",
            name="后来新增但不应进入冻结结果",
            operation_status="ENABLE",
            review_status="APPROVED",
            observed_at=datetime.now(UTC),
            published_version=1,
            source_connection_id=case.primary.connection_id,
            source_channel="OFFICIAL_API",
        )
    )
    session.flush()

    selection = freeze_selection(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        request=SelectionRequest(
            snapshot_id=page.snapshot.snapshot_id,
            mode="ALL_MATCHING",
        ),
    )
    assert {item.remote_id for item in selection.refs} == {
        ref.remote_id for ref in case.campaign_refs
    }
    assert "campaign-added-after-snapshot" not in {
        item.remote_id for item in selection.refs
    }

    export = create_export(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="b6-workspace-export",
    )
    # 直接调用本地 worker 函数，证明导出消费的是冻结副本且没有平台请求。
    from app.modules.reporting.query_models import ReportExport

    persisted = session.get(ReportExport, export.id)
    assert persisted is not None
    generate_export(session, persisted)
    session.flush()
    csv = download_export(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        export_id=export.id,
    ).decode()
    assert csv.splitlines()[0].startswith("row_key,")
    assert len(csv.splitlines()) == 4
    assert "campaign-added-after-snapshot" not in csv

    from tests.modules.reporting.query_helpers import finish_projections

    finish_projections(session)
    material_rows = build_rows(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        filters=_filters("material"),
    )
    video = next(row for row in material_rows if row.material_uses)
    assert video.metric_buckets[0].values["spend"] == Decimal("7")
    assert video.material_uses[0].platform_material_id == "vid-external-1"
    assert any(
        "RESIDUAL" in row.coverage.get("attribution_statuses", [])
        for row in material_rows
    )

    other_page = query_ads(
        session,
        context=case.other.context,
        bc_id=case.other.bc_id,
        filters=filters,
        limit=100,
    )
    assert other_page.total == 1
    assert other_page.items[0].display["operation_status"] == "ENABLE"
    with pytest.raises(HTTPException):
        # 另一个租户的 BC 标识不能在当前租户下探测到数据或快照。
        query_ads(
            session,
            context=case.primary.context,
            bc_id=case.other.bc_id,
            filters=filters,
        )


def test_workspace_capacity_has_no_n_plus_one(
    workspace_case: _WorkspaceCase, session: Session
) -> None:
    """合成 1,000 系列/10,000 广告只作为查询容量证据，不代表线上规模。"""
    case = workspace_case
    scope = case.primary
    now = datetime.now(UTC)
    campaigns: list[AdObject] = []
    ads: list[AdObject] = []
    for index in range(10_000):
        campaign_index = index // 10 + 10_000
        campaign_id = f"capacity-campaign-{campaign_index}"
        if index % 10 == 0:
            campaigns.append(
                AdObject(
                    tenant_id=scope.context.tenant_id,
                    advertiser_id=scope.advertiser_id,
                    kind="campaign",
                    remote_id=campaign_id,
                    ad_type="REGULAR",
                    name=f"capacity-series-{campaign_index}",
                    operation_status="ENABLE",
                    review_status="APPROVED",
                    observed_at=now,
                    published_version=1,
                    source_connection_id=scope.connection_id,
                    source_channel="OFFICIAL_API",
                )
            )
        ads.append(
            AdObject(
                tenant_id=scope.context.tenant_id,
                advertiser_id=scope.advertiser_id,
                kind="ad",
                remote_id=f"capacity-ad-{index}",
                parent_kind="campaign",
                parent_remote_id=campaign_id,
                ad_type="REGULAR",
                name=f"capacity-ad-{index}",
                operation_status="ENABLE",
                review_status="APPROVED",
                observed_at=now,
                published_version=1,
                source_connection_id=scope.connection_id,
                source_channel="OFFICIAL_API",
            )
        )
    session.add_all([*campaigns, *ads])
    session.flush()
    capacity_campaign_ids = session.exec(
        select(AdObject.remote_id).where(
            AdObject.tenant_id == scope.context.tenant_id,
            AdObject.advertiser_id == scope.advertiser_id,
            AdObject.kind == "campaign",
            col(AdObject.remote_id).like("capacity-campaign-%"),
        )
    ).all()
    capacity_ad_ids = session.exec(
        select(AdObject.remote_id).where(
            AdObject.tenant_id == scope.context.tenant_id,
            AdObject.advertiser_id == scope.advertiser_id,
            AdObject.kind == "ad",
            col(AdObject.remote_id).like("capacity-ad-%"),
        )
    ).all()
    assert len(capacity_campaign_ids) == 1_000
    assert len(capacity_ad_ids) == 10_000

    statements = 0

    def count_sql(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    started = time.perf_counter()
    try:
        # 合成对象大多没有报表事实；容量证据针对列出目录全部对象的路径。
        rows = build_rows(
            session,
            context=scope.context,
            bc_id=scope.bc_id,
            filters=_filters("campaign").model_copy(update={"include_empty": True}),
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert len(rows) >= 1_000
    # A fixed number of set-based reads is expected. A query per series/ad would
    # scale with input rows and is an N+1 regression even if the result is correct.
    print(  # noqa: T201
        f"B6 capacity: campaigns=1000 ads=10000 sql_statements={statements} elapsed_ms={elapsed_ms:.2f}"
    )
    assert statements <= 30, f"{statements} SQL statements for 1,000 series/10,000 ads"
    assert elapsed_ms < 30_000, (
        f"synthetic capacity query exceeded 30s: {elapsed_ms:.1f}ms"
    )
