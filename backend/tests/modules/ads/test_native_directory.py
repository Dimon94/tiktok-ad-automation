"""Native recognition through existing HTTP detail owner (Spec Testing 1–6/9)."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import business_api_client as sdk
import pytest
from urllib3.response import HTTPResponse

from app.integrations.tiktok.adapters.ads_read import FinanceReadState
from app.integrations.tiktok.adapters.sdk_ads import SdkAdsReadOperations
from app.integrations.tiktok.contracts.ads import DirectoryQuery
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.ads.models import AdObject
from app.modules.ads.sync import publish_directory, stage_directory_page
from tests.modules.ads.test_directory_navigation import navigation as navigation
from tests.modules.ads.test_directory_sync import _route, _run


def test_native_group_and_unknown_fields(navigation):
    client, seed, session = navigation
    group = session.get(
        AdObject, (seed.context.tenant_id, "account-a", "adgroup", "g1")
    )
    group.configuration = {
        "native_series_id": "series-1",
        "native_series_name": "",
        "identity_type": "BC_AUTH_TT",
        "identity_id": "group-id",
        "identity_authorized_bc_id": "group-bc",
        "placements": [],
        "unmapped": 42,
    }
    session.flush()
    response = client.get(
        f"/api/tenants/{seed.context.tenant_id}/ads/ad/a1",
        params={"bc_id": seed.bc_id, "advertiser_id": "account-a"},
    )
    assert response.status_code == 200
    detail = response.json()
    native = detail["native"]
    assert native["series_id"] == "series-1"
    assert native["series_name"] == ""
    assert native["group_identity"]["identity_id"] == "group-id"
    assert native["placements"] == []
    assert native["tiktok_subplacements"] is None
    assert native["parent_adgroup"]["ref"]["remote_id"] == "g1"
    assert native["parent_adgroup"]["configuration"]["unmapped"] == 42
    assert native["creatives"][0]["creative_id"] == "cr1"
    assert native["creatives"][0]["identity"] is None
    assert native["coverage_reasons"]
    group.configuration = {"destination_type": "MINIS"}
    session.flush()
    assert (
        client.get(
            f"/api/tenants/{seed.context.tenant_id}/ads/adgroup/g1",
            params={"bc_id": seed.bc_id, "advertiser_id": "account-a"},
        ).json()["native"]
        is None
    )


@pytest.fixture
def native_published(navigation, monkeypatch, request):
    client, seed, session = navigation
    from app.modules.accounts.connection_models import ConnectionAuthorization

    session.add(
        ConnectionAuthorization(
            tenant_id=seed.context.tenant_id,
            connection_id=seed.connection.id,
            authorization_revision=seed.connection.authorization_revision,
            source="OFFLINE_TEST",
            verified_at=datetime.now(UTC),
            permission_summary={"read_authorized": True},
        )
    )
    seed.grant.checked_at = datetime.now(UTC)
    session.flush()
    rows = {
        "campaign": [
            {
                "advertiser_id": "account-a",
                "campaign_id": "c1",
                "campaign_name": "重复名称",
            }
        ],
        "adgroup": [
            {
                "advertiser_id": "account-a",
                "adgroup_id": f"g{i}",
                "campaign_id": "c1",
                "adgroup_name": "重复名称",
                "promotion_type": "NATIVE_SERIES",
                "native_series_id": f"series-{i}",
                "identity_type": "BC_AUTH_TT",
                "identity_id": f"group-{i}",
                "identity_authorized_bc_id": f"group-bc-{i}",
                "placement_type": "PLACEMENT_TYPE_NORMAL",
                "placements": ["PLACEMENT_TIKTOK"],
                "tiktok_subplacements": [],
                "unmapped": "kept",
            }
            for i in (1, 2)
        ],
        "ad": [
            {
                "advertiser_id": "account-a",
                "smart_plus_ad_id": "a1",
                "adgroup_id": "g1",
                "ad_name": "重复名称",
                "creative_list": [
                    {
                        "smart_plus_creative_id": f"cr{i}",
                        "ad_material_id": f"am{i}",
                        "creative_info": {
                            "identity_type": "BC_AUTH_TT",
                            "identity_id": f"creative-{i}",
                            "identity_authorized_bc_id": f"creative-bc-{i}",
                            "video_info": {"video_id": "shared-video"},
                            "material_name": f"material-{i}",
                        },
                    }
                    for i in (2, 1)
                ],
            }
        ],
        "creative": [
            {
                "advertiser_id": "account-a",
                "ad_id": f"cr{i}",
                "smart_plus_ad_id": "a1",
                "adgroup_id": "g1",
                "ad_name": "重复名称",
                "identity_type": "BC_AUTH_TT",
                "identity_id": f"creative-{i}",
                "identity_authorized_bc_id": f"creative-bc-{i}",
                "video_id": "shared-video",
            }
            for i in (1, 2)
        ],
    }
    partial = getattr(request, "param", None)
    if partial == "null-list":
        rows["ad"][0]["creative_list"] = None
    elif partial == "null-info":
        rows["creative"][0] = {
            "advertiser_id": "account-a",
            "ad_id": "cr1",
            "smart_plus_ad_id": "a1",
            "ad_name": "partial-child",
            "creative_info": None,
        }
    calls = []
    active = []

    def transport(_pool, method, url, **_kwargs):
        calls.append((method, url))
        values = active.pop(0)
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "list": values,
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_page": 1,
                            "total_number": len(values),
                        },
                    },
                    "request_id": "synthetic-native",
                }
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    api = sdk.ApiClient()
    api.default_headers["Access-Token"] = "synthetic-offline-token"
    from app.modules.accounts.routing import verify_route

    route = FrozenTikTokRoute(**_route(seed))

    def check_account(account):
        verify_route(
            session,
            context=seed.context,
            route=route,
            advertiser_id=account,
            capability="read",
        )
        return account

    @contextmanager
    def request_scope(account, _operation, _deadline):
        check_account(account)
        yield

    adapter = SdkAdsReadOperations(
        api,
        route=route,
        check_account=check_account,
        check_accounts=lambda accounts: {a: check_account(a) for a in accounts},
        subject_id=None,
        finance=FinanceReadState(),
        request_scope=request_scope,
        deadline=datetime.now(UTC) + timedelta(seconds=60),
    )
    for kind in ("campaign", "adgroup", "ad", "creative"):
        active[:] = [rows[kind]]
        page = adapter.read_primary_page(
            DirectoryQuery("account-a", kind, "SMART_PLUS", 1, 100, (), (), False)
        )
        run = _run(session, seed, kind=kind, ad_type="SMART_PLUS")
        stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
        publish_directory(session, run_id=run.id, claim_generation=1)
    assert len(calls) == 4 and all(method == "GET" for method, _ in calls)
    session.refresh(seed.row)
    yield navigation


@pytest.mark.parametrize(
    "native_published,partial_field",
    [("null-list", "creative_list"), ("null-info", "creative_info")],
    indirect=["native_published"],
)
def test_published_unknown_creative_fields_remain_readable(
    native_published, partial_field
):
    client, seed, _session = native_published
    response = client.get(
        f"/api/tenants/{seed.context.tenant_id}/ads/ad/a1",
        params={"bc_id": seed.bc_id, "advertiser_id": "account-a"},
    )
    assert response.status_code == 200
    detail = response.json()
    native = detail["native"]
    assert native["series_id"] == "series-1"
    assert native["group_identity"]["identity_id"] == "group-1"
    assert {c["creative_id"] for c in native["creatives"]} == {"cr1", "cr2"}
    assert "creative_coverage:unknown" in native["coverage_reasons"]
    if partial_field == "creative_list":
        assert detail["configuration"]["creative_list"] is None
        assert "creative_list:not_provided" in native["coverage_reasons"]
    else:
        creative = next(c for c in native["creatives"] if c["creative_id"] == "cr1")
        assert creative["configuration"]["creative_info"] is None
        assert creative["identity"] is None
        assert "creative_identity:not_provided" in creative["coverage_reasons"]


def test_native_transport_published_http(native_published):
    client, seed, session = native_published
    base = f"/api/tenants/{seed.context.tenant_id}"
    params = {"bc_id": seed.bc_id, "advertiser_id": "account-a"}
    for i in (1, 2):
        detail = client.get(base + f"/ads/adgroup/g{i}", params=params).json()
        assert detail["native"]["series_id"] == f"series-{i}"
        assert detail["native"]["group_identity"]["identity_id"] == f"group-{i}"
    detail = client.get(base + "/ads/ad/a1", params=params).json()
    native = detail["native"]
    assert native["series_id"] == "series-1"
    assert native["group_identity"]["identity_authorized_bc_id"] == "group-bc-1"
    assert native["tiktok_subplacements"] == []
    assert len(native["creatives"]) == 2
    for creative in native["creatives"]:
        i = creative["creative_id"][-1]
        assert creative["identity"]["identity_id"] == f"creative-{i}"
        assert creative["identity"]["identity_authorized_bc_id"] == f"creative-bc-{i}"
        assert [m["platform_material_id"] for m in creative["materials"]] == [
            "shared-video"
        ]
        assert creative["published_version"] and creative["observed_at"]
    assert (
        detail["configuration"]["creative_list"][0]["smart_plus_creative_id"] == "cr2"
    )
    assert detail["source_channel"] == "OFFICIAL_API"
    assert "creative_coverage:unknown" in native["coverage_reasons"]


def test_native_workspace_live_http(native_published):
    # Reuse the actual FastAPI/Vite/Playwright host, without response interception.
    import os

    from tests.modules.ads.test_directory_navigation import (
        test_directory_workspace_live_http,
    )

    os.environ["DIRECTORY_NATIVE"] = "1"
    try:
        test_directory_workspace_live_http(native_published)
    finally:
        os.environ.pop("DIRECTORY_NATIVE", None)


def test_partial_observations_and_exact_creative_identity(native_published):
    client, seed, session = native_published
    from app.modules.ads.creative_models import AdCreative

    ad = session.get(AdObject, (seed.context.tenant_id, "account-a", "ad", "a1"))
    group = session.get(
        AdObject, (seed.context.tenant_id, "account-a", "adgroup", "g1")
    )
    group.configuration = {"promotion_type": "NATIVE_SERIES", "native_series_name": ""}
    group.observed_at = datetime(2020, 1, 1, tzinfo=UTC)
    ad.configuration = {
        "creative_list": [
            {
                "smart_plus_creative_id": "cr2",
                "creative_info": {"identity_id": "inline-two"},
            },
            {"creative_info": {"identity_id": "anonymous-identity"}},
            {"smart_plus_creative_id": "missing", "creative_info": {"identity_id": ""}},
        ],
        "extra_unknown": {"kept": True},
    }
    fact = session.get(AdCreative, (seed.context.tenant_id, "account-a", "cr1"))
    fact.assets = [
        {"platform_material_id": "shared-video", "material_type": "VIDEO"},
        {"platform_material_id": "second-video", "material_type": "VIDEO"},
    ]
    fact.observed_at = datetime(2021, 1, 1, tzinfo=UTC)
    fact.published_version = 99
    session.flush()
    params = {"bc_id": seed.bc_id, "advertiser_id": "account-a"}
    url = f"/api/tenants/{seed.context.tenant_id}/ads/ad/a1"
    detail = client.get(url, params=params).json()
    native = detail["native"]
    assert native["series_id"] is None and native["series_name"] == ""
    assert native["group_identity"] is None and native["tiktok_subplacements"] is None
    assert native["parent_adgroup"]["observed_at"].startswith("2020-01-01")
    entries = {c["creative_id"]: c for c in native["creatives"]}
    assert entries["cr1"]["identity"]["identity_id"] == "creative-1"
    assert entries["cr2"]["identity"]["identity_id"] == "creative-2"
    assert entries[None]["identity"]["identity_id"] == "anonymous-identity"
    assert entries["missing"]["identity"]["identity_id"] == ""
    assert entries["missing"]["materials"] == []
    assert "creative_materials:not_covered" in entries["missing"]["coverage_reasons"]
    assert [m["platform_material_id"] for m in entries["cr1"]["materials"]] == [
        "shared-video",
        "second-video",
    ]
    assert entries["cr1"]["materials"][1]["name"] is None
    assert entries["cr1"]["materials"][0]["published_version"] == 99
    assert entries["cr1"]["materials"][0]["observed_at"].startswith("2021-01-01")
    assert detail["configuration"]["extra_unknown"] == {"kept": True}
    # 快照只验证报表上下文，不能把当前目录变成历史切片。
    from app.modules.reporting.query_models import QuerySnapshot

    snapshot = QuerySnapshot(
        tenant_id=seed.context.tenant_id,
        bc_id=seed.bc_id,
        actor_id=seed.context.actor_id,
        advertiser_ids=["account-a"],
        filter_digest="f" * 64,
        publication_versions={"directory": 1},
    )
    session.add(snapshot)
    session.flush()
    with_snapshot = client.get(url, params=params | {"snapshot_id": str(snapshot.id)})
    assert with_snapshot.status_code == 200
    assert with_snapshot.json()["native"] == native

    ad.parent_remote_id = "not-synced"
    ad.configuration = {"promotion_type": "NATIVE_SERIES"}
    session.flush()
    orphan = client.get(url, params=params).json()
    assert orphan["parent"]["ref"]["remote_id"] == "not-synced"
    assert orphan["native"]["series_id"] is None
    assert "parent_not_synced" in orphan["native"]["coverage_reasons"]
