"""目录双通道只在 HTTP 边界替身，授权与准入使用真实 PostgreSQL/Redis。"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import DirectoryQuery
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from tests.integrations.tiktok.gateway_support import business_calls, gateway
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy


def query(case, kind="ad", ad_type="REGULAR", **kwargs):
    return DirectoryQuery(
        case[2],
        kind,
        ad_type,
        kwargs.get("page", 1),
        10,
        kwargs.get("ids", ()),
        (),
        kwargs.get("include_deleted", False),
    )


def page(rows, number=1, size=10, total=None):
    count = len(rows) if total is None else total
    return {
        "list": rows,
        "page_info": {
            "page": number,
            "page_size": size,
            "total_page": max(1, (count + size - 1) // size),
            "total_number": count,
        },
    }


def replies(wire, channel, by_tool):
    if channel == "OFFICIAL_MCP":
        for tool, values in by_tool.items():
            for data in values:
                wire["wire"].results[tool].append(
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "code": 0,
                                        "data": data,
                                        "request_id": "ads-request",
                                    }
                                ),
                            }
                        ]
                    }
                )
    else:
        # Reuse the real SDK transport fixture; choose response at the existing physical send hook.
        original = wire["before"]["callback"]

        def choose():
            if original:
                original()
            # SDK fixture records the request after this hook, hence responses are ordered.
            wire["sdk_data"]["data"] = sequence.pop(0)

        sequence = [value for values in by_tool.values() for value in values]
        wire["before"]["callback"] = choose


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize("ad_type", ["REGULAR", "SMART_PLUS"])
def test_directory_three_levels(
    database_engine, redis_client, gateway_case, gateway_wire, kind, ad_type
):
    tool = ("smart_plus_" if ad_type == "SMART_PLUS" else "") + kind + "_get"
    key = "smart_plus_ad_id" if tool == "smart_plus_ad_get" else kind + "_id"
    row = {
        "advertiser_id": gateway_case[2],
        key: "object-7",
        kind + "_name": "外部广告",
        "campaign_id": "parent-c",
        "adgroup_id": "parent-g",
        "operation_status": "ENABLE",
    }
    row[key] = "object-7"
    if kind == "ad":
        if ad_type == "SMART_PLUS":
            row["creative_list"] = [
                {
                    "ad_material_id": "ad-material-9",
                    "material_operation_status": "DISABLE",
                    "smart_plus_creative_id": "selected-creative",
                    "creative_info": {
                        "material_name": "素材名称",
                        "video_info": {"video_id": "video-11"},
                    },
                }
            ]
        else:
            row["video_id"] = "video-11"
    data = {tool: [page([row])]}
    if tool == "smart_plus_ad_get":
        data["ad_get"] = [
            page(
                [
                    {
                        "advertiser_id": gateway_case[2],
                        "ad_id": "creative-8",
                        "smart_plus_ad_id": "object-7",
                        "campaign_automation_type": "UPGRADED_SMART_PLUS_CREATIVE",
                        "adgroup_id": "parent-g",
                        "video_id": "video-auto",
                    }
                ],
                size=1000,
            )
        ]
    replies(gateway_wire, gateway_case[1].channel, data)
    with gateway(database_engine, redis_client, gateway_case) as client:
        try:
            result = client.ads.read_page(query(gateway_case, kind, ad_type))
        except DomainError as error:
            pytest.fail(error.code)
    assert result.page == 1 and result.complete and result.next_page is None
    assert result.items[0].ref.remote_id == "object-7"
    if kind == "ad":
        assert result.materials[0].use_ref.platform_material_id == "video-11"
        assert result.materials[0].use_ref.ad_material_id == (
            "ad-material-9" if ad_type == "SMART_PLUS" else None
        )
        if ad_type == "SMART_PLUS":
            assert result.materials[0].operation_status == "DISABLE"
            assert result.materials[0].name == "素材名称"
            assert result.materials[0].creative_ids == ("selected-creative",)
            assert result.items[1].ref.kind == "creative"
            assert result.items[1].parent_ref == result.items[0].ref
            assert result.materials[1].creative_ids == ("creative-8",)
            assert result.materials[1].use_ref.ad_material_id is None


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_missing_finance_subject_never_sends(
    database_engine, redis_client, gateway_case, gateway_wire
):
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_balance(gateway_case[2])
    assert result.amount is None and result.availability == "PERMISSION_UNVERIFIED"
    assert result.balance_scope == "UNKNOWN"
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_every_page_rechecks_binding(
    database_engine, redis_client, gateway_case, gateway_wire
):
    replies(
        gateway_wire, gateway_case[1].channel, {"campaign_get": [page([], total=0)]}
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        client.ads.read_page(query(gateway_case, "campaign"))
        with Session(database_engine) as session:
            row = session.get(
                BCConnectionBinding,
                (
                    gateway_case[0].tenant_id,
                    gateway_case[1].bc_id,
                    gateway_case[1].connection_id,
                ),
            )
            row.revision += 1
            session.add(row)
            session.commit()
        with pytest.raises(DomainError) as error:
            client.ads.read_page(query(gateway_case, "campaign", page=2))
        assert error.value.code == "route_binding_changed"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1


def authenticated_subject(engine, case):
    with Session(engine) as session:
        row = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == case[1].connection_id
            )
        ).one()
        row.upstream_subject = "subject-1"
        session.add(row)
        session.commit()


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "finance_role,relation",
    [
        ("MANAGER", "BOUND"),
        ("ANALYST", "BOUND"),
        (None, "BOUND"),
        ("MANAGER", "UNBOUND"),
    ],
)
def test_finance_separate_permissions_and_missing_amount(
    database_engine, redis_client, gateway_case, gateway_wire, finance_role, relation
):
    authenticated_subject(database_engine, gateway_case)
    member = {
        "user_id": "subject-1",
        "user_role": "ADMIN",
        "relation_status": relation,
        "ext_user_role": {"finance_role": finance_role},
    }
    balance = {
        "advertiser_account_list": [
            {"advertiser_id": gateway_case[2], "currency": "USD", "balance_info": {}}
        ],
        "page_info": page([{}], size=1)["page_info"],
    }
    replies(
        gateway_wire,
        gateway_case[1].channel,
        {
            "bc_member_get": [page([member], size=20)],
            "advertiser_balance_get": [balance],
        },
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_balance(gateway_case[2])
    assert result.amount is None
    granted = finance_role in {"MANAGER", "ANALYST"} and relation == "BOUND"
    assert result.availability == (
        "BALANCE_MISSING" if granted else "PERMISSION_UNVERIFIED"
    )
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == (
        2 if granted else 1
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("scope", ["ADVERTISER", "PORTFOLIO", "UNKNOWN"])
def test_balance_exact_numeric_scope_and_persistence(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch, scope
):
    from sqlmodel import delete
    from urllib3.response import HTTPResponse

    from app.modules.ads.balances import persist_balance
    from app.modules.reporting.models import AccountBalanceObservation

    authenticated_subject(database_engine, gateway_case)
    member = {
        "user_id": "subject-1",
        "relation_status": "BOUND",
        "ext_user_role": {"finance_role": "MANAGER"},
    }
    row = {
        "advertiser_id": gateway_case[2],
        "currency": "USD",
        "account_balance": "EXACT_NUMBER",
    }
    if scope == "ADVERTISER":
        row["balance_info"] = {"account_balance": "EXACT_NUMBER"}
    elif scope == "PORTFOLIO":
        row["payment_portfolio_id"] = 90071992547409931
    balance = {
        "advertiser_account_list": [row],
        "page_info": page([row], size=1)["page_info"],
    }

    def envelope(data):
        return json.dumps({"code": 0, "data": data}).replace(
            '"EXACT_NUMBER"', "123456789.123456789012345678901"
        )

    bodies = [envelope(page([member], size=20)), envelope(balance)]
    if gateway_case[1].channel == "OFFICIAL_MCP":
        for tool, body in zip(
            ["bc_member_get", "advertiser_balance_get"], bodies, strict=True
        ):
            gateway_wire["wire"].results[tool].append(
                {"content": [{"type": "text", "text": body}]}
            )
    else:

        def send(_pool, method, url, **kwargs):
            gateway_wire["sdk_calls"].append((method, url, kwargs))
            return HTTPResponse(
                body=bodies.pop(0).encode(),
                status=200,
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr("urllib3.PoolManager.request", send)
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_balance(gateway_case[2])
    assert result.balance_scope == scope
    assert result.amount == (
        None if scope == "UNKNOWN" else Decimal("123456789.123456789012345678901")
    )
    calls = business_calls(gateway_wire, gateway_case[1].channel)
    if gateway_case[1].channel == "OFFICIAL_MCP":
        args = calls[-1]["params"]["arguments"]
        assert args == {
            "bc_id": gateway_case[1].bc_id,
            "page": 1,
            "page_size": 1,
            "fields": ["balance_info"],
        }
    else:
        assert calls[-1][1].endswith("/advertiser/balance/get/")
        assert dict(calls[-1][2]["fields"])["fields"] == '["balance_info"]'
    with Session(database_engine) as session:
        saved = persist_balance(
            session,
            context=gateway_case[0],
            route=gateway_case[1],
            advertiser_id=gateway_case[2],
            balance=result,
        )
        session.commit()
        session.refresh(saved)
        assert saved.amount == result.amount and saved.balance_scope == scope
        assert saved.evidence["route"]["bc_id"] == gateway_case[1].bc_id
        session.exec(
            delete(AccountBalanceObservation).where(
                AccountBalanceObservation.id == saved.id
            )
        )
        session.commit()


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_read_denial_never_borrows_build_grant(
    database_engine, redis_client, gateway_case, gateway_wire
):
    with Session(database_engine) as session:
        row = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == gateway_case[1].connection_id
            )
        ).one()
        row.permission_summary = {"read_authorized": False, "build_authorized": True}
        session.add(row)
        session.commit()
    with gateway(database_engine, redis_client, gateway_case) as client:
        with pytest.raises(DomainError) as error:
            client.ads.read_page(query(gateway_case))
        assert error.value.code == "account_access_denied"
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("damage", ["page", "account", "parent", "id"])
def test_malformed_page_or_identity_rejected(
    database_engine, redis_client, gateway_case, gateway_wire, damage
):
    row = {"advertiser_id": gateway_case[2], "ad_id": "ad-1", "adgroup_id": "group-1"}
    data = page([row])
    q = replace(query(gateway_case, ids=("ad-1",)), parent_ids=("group-1",))
    if damage == "page":
        data["page_info"]["page"] = 2
    elif damage == "account":
        row["advertiser_id"] = "another-account"
    elif damage == "parent":
        row["adgroup_id"] = "another-group"
    else:
        row["ad_id"] = "another-ad"
    replies(gateway_wire, gateway_case[1].channel, {"ad_get": [data]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        with pytest.raises(DomainError) as error:
            client.ads.read_page(q)
        assert error.value.code == "ads_response_invalid"


@pytest.mark.parametrize("damage", ["missing", "drift"])
@pytest.mark.parametrize("where", ["persisted", "current"])
def test_mcp_requested_tool_unavailable_other_directory_still_works(
    database_engine, redis_client, gateway_case, gateway_wire, damage, where
):
    from app.modules.accounts.connection_models import ConnectionToolObservation

    if where == "current":
        tools = gateway_wire["wire"].tools
        target = next(tool for tool in tools if tool["name"] == "ad_get")
        if damage == "missing":
            tools.remove(target)
        else:
            target["inputSchema"]["required"] = ["advertiser_id", "unknown_field"]
    else:
        with Session(database_engine) as session:
            observation = session.exec(
                select(ConnectionToolObservation).where(
                    ConnectionToolObservation.connection_id
                    == gateway_case[1].connection_id
                )
            ).one()
            schemas = json.loads(json.dumps(observation.tool_schemas))
            if damage == "missing":
                del schemas["ad_get"]
            else:
                schemas["ad_get"]["inputSchema"]["required"] = [
                    "advertiser_id",
                    "unknown_field",
                ]
            observation.tool_schemas = schemas
            session.add(observation)
            session.commit()
    replies(gateway_wire, "OFFICIAL_MCP", {"smart_plus_campaign_get": [page([])]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        assert client.ads.read_page(
            query(gateway_case, "campaign", "SMART_PLUS")
        ).complete
        with pytest.raises(DomainError) as error:
            client.ads.read_page(query(gateway_case))
        assert error.value.code == (
            "mcp_tool_unavailable" if damage == "missing" else "mcp_contract_changed"
        )
        assert error.value.effect == "NOT_SENT"
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_auto_creative_unjoinable_is_incomplete(
    database_engine, redis_client, gateway_case, gateway_wire
):
    ad = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "smart-1",
        "adgroup_id": "group-1",
        "creative_list": [],
    }
    creative = {
        "advertiser_id": gateway_case[2],
        "adgroup_id": "group-1",
        "ad_id": "creative-1",
        "video_id": "auto-video",
    }
    replies(
        gateway_wire,
        gateway_case[1].channel,
        {"smart_plus_ad_get": [page([ad])], "ad_get": [page([creative], size=1000)]},
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(query(gateway_case, ad_type="SMART_PLUS"))
    assert result.complete and not result.materials_complete
    assert (
        not result.materials
        and result.material_missing_reason == "CREATIVE_ASSOCIATION_INCOMPLETE"
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "ad_type,automation",
    [("REGULAR", "MANUAL"), ("LEGACY_SMART_PLUS", "SMART_PLUS"), ("SMART_PLUS", None)],
)
def test_deleted_filter_real_page_and_exact_wire(
    database_engine, redis_client, gateway_case, gateway_wire, ad_type, automation
):
    tool = "smart_plus_campaign_get" if ad_type == "SMART_PLUS" else "campaign_get"
    row = {
        "advertiser_id": gateway_case[2],
        "campaign_id": "campaign-21",
        "operation_status": "DELETE",
    }
    replies(
        gateway_wire, gateway_case[1].channel, {tool: [page([row], number=3, total=21)]}
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(
            query(gateway_case, "campaign", ad_type, page=3, include_deleted=True)
        )
    assert result.page == 3 and result.complete
    assert result.items[0].operation_status == "DELETE"
    calls = business_calls(gateway_wire, gateway_case[1].channel)
    if gateway_case[1].channel == "OFFICIAL_MCP":
        arguments = calls[0]["params"]["arguments"]
    else:
        arguments = dict(calls[0][2]["fields"])
        arguments["filtering"] = json.loads(arguments["filtering"])
    assert arguments["page"] == 3 and arguments["page_size"] == 10
    expected = {"primary_status": "STATUS_ALL"}
    if automation:
        expected["campaign_automation_type"] = automation
    assert arguments["filtering"] == expected


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_financial_pagination_filters_target_and_keeps_bc_scope(
    database_engine, redis_client, gateway_case, gateway_wire
):
    authenticated_subject(database_engine, gateway_case)
    others = [{"user_id": f"other-{n}"} for n in range(20)]
    member = {
        "user_id": "subject-1",
        "relation_status": "BOUND",
        "ext_user_role": {"finance_role": "ANALYST"},
    }
    other = {
        "advertiser_id": "not-authorized-target",
        "currency": "USD",
        "balance_info": {"account_balance": "9000"},
    }
    target = {
        "advertiser_id": gateway_case[2],
        "currency": "USD",
        "balance_info": {"account_balance": "0"},
    }
    finances = [
        {
            "advertiser_account_list": [row],
            "page_info": page([row], number=n, size=1, total=2)["page_info"],
        }
        for n, row in enumerate([other, target], 1)
    ]
    replies(
        gateway_wire,
        gateway_case[1].channel,
        {
            "bc_member_get": [
                page(others, size=20, total=21),
                page([member], number=2, size=20, total=21),
            ],
            "advertiser_balance_get": finances,
        },
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_balance(gateway_case[2])
    assert result.amount == Decimal("0") and result.availability == "AVAILABLE"
    assert result.scope_id == gateway_case[2]
    calls = business_calls(gateway_wire, gateway_case[1].channel)
    assert len(calls) == 4
    for call in calls:
        args = (
            call["params"]["arguments"]
            if gateway_case[1].channel == "OFFICIAL_MCP"
            else dict(call[2]["fields"])
        )
        assert args["bc_id"] == gateway_case[1].bc_id
        assert "advertiser_id" not in args


def test_missing_auto_creative_tool_preserves_directory_but_marks_material_incomplete(
    database_engine, redis_client, gateway_case, gateway_wire
):
    gateway_wire["wire"].tools = [
        tool for tool in gateway_wire["wire"].tools if tool["name"] != "ad_get"
    ]
    ad = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "smart-1",
        "adgroup_id": "group-1",
        "creative_list": [],
    }
    replies(gateway_wire, "OFFICIAL_MCP", {"smart_plus_ad_get": [page([ad])]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(query(gateway_case, ad_type="SMART_PLUS"))
    assert result.complete and not result.materials_complete
    assert result.material_missing_reason == "mcp_tool_unavailable"
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_unsupported_type_or_oversized_smart_page_never_sends(
    database_engine, redis_client, gateway_case, gateway_wire
):
    with gateway(database_engine, redis_client, gateway_case) as client:
        for q, code in [
            (query(gateway_case, ad_type="UNKNOWN_OLD_TYPE"), "ads_type_unavailable"),
            (
                replace(query(gateway_case, ad_type="SMART_PLUS"), page_size=101),
                "ads_query_invalid",
            ),
        ]:
            with pytest.raises(DomainError) as error:
                client.ads.read_page(q)
            assert error.value.code == code
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


def test_member_read_rejection_does_not_retire_directory(
    database_engine, redis_client, gateway_case, gateway_wire
):
    authenticated_subject(database_engine, gateway_case)
    gateway_wire["wire"].results["bc_member_get"].append(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"code": 40001, "data": {}, "request_id": "denied-member"}
                    ),
                }
            ]
        }
    )
    replies(gateway_wire, "OFFICIAL_MCP", {"campaign_get": [page([])]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        balance = client.ads.read_balance(gateway_case[2])
        assert balance.amount is None and balance.availability == "mcp_business_error"
        assert client.ads.read_page(query(gateway_case, "campaign")).complete
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 2


@pytest.mark.parametrize("effect,code", [("WRITE", 40001), ("READ", "0")])
def test_non_reusable_mcp_failure_still_retires(
    effect, code, client_factory, fixture_contract, mcp_wire
):
    from app.integrations.tiktok.contracts.common import RemoteCallError

    contract = replace(fixture_contract, effect=effect)
    mcp_wire.results[contract.tool_name].append(
        {"content": [], "structuredContent": {"code": code, "data": {}}}
    )
    with client_factory(contracts={contract.operation: contract}) as client:
        args = {
            "operation": contract.operation,
            "advertiser_id": "123",
            "arguments": {"advertiser_id": "123"},
        }
        with pytest.raises(RemoteCallError) as failure:
            client.call(**args)
        assert failure.value.effect == "UNKNOWN"
        with pytest.raises(RemoteCallError) as retired:
            client.call(**args)
        assert (
            retired.value.code == "mcp_session_unavailable"
            and retired.value.effect == "NOT_SENT"
        )
    assert (
        len([call for call in mcp_wire.calls if call.get("method") == "tools/call"])
        == 1
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_current_subject_change_cannot_borrow_old_finance_proof(
    database_engine, redis_client, gateway_case, gateway_wire
):
    authenticated_subject(database_engine, gateway_case)
    member = {
        "user_id": "subject-1",
        "relation_status": "BOUND",
        "ext_user_role": {"finance_role": "MANAGER"},
    }
    replies(
        gateway_wire,
        gateway_case[1].channel,
        {"bc_member_get": [page([member], size=20)]},
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        with Session(database_engine) as session:
            row = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id
                    == gateway_case[1].connection_id
                )
            ).one()
            row.upstream_subject = "different-subject"
            session.add(row)
            session.commit()
        with pytest.raises(DomainError) as error:
            client.ads.read_balance(gateway_case[2])
        assert error.value.code == "account_access_denied"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_multiple_native_creatives_preserve_one_material_use(
    database_engine, redis_client, gateway_case, gateway_wire
):
    ad = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "smart-1",
        "adgroup_id": "group-1",
        "creative_list": [],
    }
    creatives = [
        {
            "advertiser_id": gateway_case[2],
            "smart_plus_ad_id": "smart-1",
            "campaign_automation_type": "UPGRADED_SMART_PLUS_CREATIVE",
            "ad_id": creative,
            "adgroup_id": "group-1",
            "video_id": "same-video",
        }
        for creative in ("creative-a", "creative-b")
    ]
    replies(
        gateway_wire,
        gateway_case[1].channel,
        {"smart_plus_ad_get": [page([ad])], "ad_get": [page(creatives, size=1000)]},
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(query(gateway_case, ad_type="SMART_PLUS"))
    assert result.materials_complete
    assert len(result.materials) == 1
    assert result.materials[0].creative_ids == ("creative-a", "creative-b")
    assert result.materials[0].use_ref.ad_material_id is None


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_post_only_material_and_video_identity_do_not_collide(
    database_engine, redis_client, gateway_case, gateway_wire
):
    creative_list = [
        {"creative_info": {"video_info": {"video_id": "90071992547409931"}}},
        {
            "smart_plus_creative_id": "post-creative",
            # 报表字段即使混入目录响应，也不能参与帖子/视频使用身份解析。
            "main_material_id": "report-only-post",
            "material_operation_status": "DISABLE",
            "creative_info": {
                "tiktok_item_id": "90071992547409931",
                "material_name": "授权帖子",
            },
        },
    ]
    ad = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "smart-1",
        "adgroup_id": "group-1",
        "creative_list": creative_list,
    }
    replies(
        gateway_wire,
        gateway_case[1].channel,
        {"smart_plus_ad_get": [page([ad])], "ad_get": [page([], size=1000)]},
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(query(gateway_case, ad_type="SMART_PLUS"))
    assert result.materials_complete and len(result.materials) == 2
    assert {item.use_ref.material_type for item in result.materials} == {
        "VIDEO",
        "TIKTOK_POST",
    }
    post = result.materials[1]
    assert post.use_ref.platform_material_id == "90071992547409931"
    assert post.use_ref.ad_material_id is None and post.local_material_id is None
    assert post.name == "授权帖子" and post.creative_ids == ("post-creative",)
    assert post.operation_status == "DISABLE"


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_standalone_smart_creative_uses_documented_parent_field(
    database_engine, redis_client, gateway_case, gateway_wire
):
    creative = {
        "advertiser_id": gateway_case[2],
        "adgroup_id": "group-1",
        "campaign_automation_type": "UPGRADED_SMART_PLUS_CREATIVE",
        "ad_id": "creative-1",
        "smart_plus_ad_id": "asset-group-1",
        "video_id": "video-1",
    }
    assert "ad_id_v2" not in creative
    replies(gateway_wire, gateway_case[1].channel, {"ad_get": [page([creative])]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(
            query(gateway_case, "creative", "SMART_PLUS", ids=("creative-1",))
        )
    assert result.complete and result.materials_complete
    entity = result.items[0]
    assert entity.ref.kind == "creative" and entity.ref.remote_id == "creative-1"
    assert (
        entity.parent_ref.kind == "ad"
        and entity.parent_ref.remote_id == "asset-group-1"
    )
    assert result.materials[0].use_ref.ad_ref == entity.parent_ref
    assert result.materials[0].creative_ids == ("creative-1",)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("damage", ["account", "group"])
def test_supplement_smart_parent_preserves_account_and_group_checks(
    database_engine, redis_client, gateway_case, gateway_wire, damage
):
    ads = [
        {
            "advertiser_id": gateway_case[2],
            "smart_plus_ad_id": f"smart-{n}",
            "adgroup_id": f"group-{n}",
            "creative_list": [],
        }
        for n in (1, 2)
    ]
    creative = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "smart-1",
        "campaign_automation_type": "UPGRADED_SMART_PLUS_CREATIVE",
        "ad_id": "creative-1",
        "adgroup_id": "group-1",
        "video_id": "video-1",
    }
    if damage == "account":
        creative["advertiser_id"] = "other-account"
    else:
        # 组仍在本页组集合中，但不属于此真实资产组；不能仅检查集合包含关系。
        creative["adgroup_id"] = "group-2"
    replies(
        gateway_wire,
        gateway_case[1].channel,
        {"smart_plus_ad_get": [page(ads)], "ad_get": [page([creative], size=1000)]},
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(query(gateway_case, ad_type="SMART_PLUS"))
    assert result.complete and not result.materials_complete
    assert result.material_missing_reason == "ads_response_invalid"
    assert len(result.items) == 2 and not result.materials


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_balance_batch_reads_one_bounded_page_and_resumes_at_requested_page(
    database_engine, redis_client, gateway_case, gateway_wire
):
    authenticated_subject(database_engine, gateway_case)
    member = {
        "user_id": "subject-1",
        "relation_status": "BOUND",
        "ext_user_role": {"finance_role": "ANALYST"},
    }
    target = {
        "advertiser_id": gateway_case[2],
        "currency": "USD",
        "balance_info": {"account_balance": "12.5"},
    }
    balance = {
        "advertiser_account_list": [target],
        "page_info": page([target], number=3, size=100, total=201)["page_info"],
    }
    replies(
        gateway_wire,
        "OFFICIAL_API",
        {
            "bc_member_get": [page([member], size=20)],
            "advertiser_balance_get": [balance],
        },
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        values, next_page = client.ads.read_balance_page((gateway_case[2],), page=3)
    assert values[gateway_case[2]].amount == Decimal("12.5") and next_page is None
    calls = business_calls(gateway_wire, "OFFICIAL_API")
    assert len(calls) == 2
    args = dict(calls[-1][2]["fields"])
    assert args["page"] == 3 and args["page_size"] == 100


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_optional_material_quota_keeps_successful_directory_page(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch, policy
):
    """真实 Redis 只放行主请求；补读没额度不能撤销已成功目录，主请求仍受准入保护。"""
    from app.core.config import settings
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    limited = policy.model_copy(update={"app_calls_per_window": 1, "window_ms": 60_000})
    monkeypatch.setattr(
        settings, "TIKTOK_CALL_POLICIES", {"base": limited.model_dump()}
    )
    # SDK 物理端点独立计速率：显式占用补读端点，不能再用主请求耗尽所有接口。
    from uuid import uuid4

    from app.integrations.tiktok.admission import SDK_READ_RATE_GROUPS
    from app.jobs.admission import admit_call, release_call

    scope = dict(  # noqa: C408
        app_scope=settings.TIKTOK_APP_ID,
        endpoint="ads.get_ads",
        tenant_id=gateway_case[0].tenant_id,
        advertiser_id=gateway_case[2],
    )
    lease = uuid4()
    assert admit_call(
        redis_client,
        **scope,
        lease_id=lease,
        policy=limited,
        rate_group=SDK_READ_RATE_GROUPS["ads.get_ads"],
    ).granted
    release_call(redis_client, **scope, lease_id=lease)
    ad = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "smart-1",
        "adgroup_id": "group-1",
        "operation_status": "ENABLE",
        "creative_list": [],
    }
    replies(gateway_wire, "OFFICIAL_API", {"smart_plus_ad_get": [page([ad])]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_page(query(gateway_case, ad_type="SMART_PLUS"))
        assert result.complete and not result.materials_complete
        assert result.items[0].ref.remote_id == "smart-1"
        assert result.items[0].operation_status == "ENABLE"
        assert result.material_missing_reason == "admission_deferred"
        # 主请求额度不足仍应直接退避，不能变成虚假的目录成功。
        with pytest.raises(AccountAdmissionDeferred):
            client.ads.read_page(query(gateway_case, ad_type="SMART_PLUS"))
    assert len(business_calls(gateway_wire, "OFFICIAL_API")) == 1


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("partial", [False, True])
def test_sdk_carousel_directory_retains_composition_and_partial_evidence(
    database_engine, redis_client, gateway_case, gateway_wire, partial
):
    from app.integrations.tiktok.contracts.carousel import decode_carousels
    from tests.modules.reporting.test_carousel_ad_links import creative

    entries = [
        creative(("shared", "second"), "song-1"),
        creative(("second", "shared"), "song-2", "native-2"),
    ]
    if partial:
        del entries[0]["creative_info"]["music_info"]
    row = {
        "advertiser_id": gateway_case[2],
        "smart_plus_ad_id": "ad",
        "adgroup_id": "group",
        "creative_list": entries,
    }
    replies(gateway_wire, "OFFICIAL_API", {"smart_plus_ad_get": [page([row])]})
    with gateway(database_engine, redis_client, gateway_case) as client:
        result = client.ads.read_primary_page(query(gateway_case, ad_type="SMART_PLUS"))
    decoded = decode_carousels(
        result.items[0].configuration,
        advertiser_id=gateway_case[2],
        ad_id="ad",
        smart=True,
    )
    assert decoded.complete is not partial
    assert decoded.identities[-1].ordered_image_ids == ("second", "shared")
    assert decoded.identities[-1].music_id == "song-2"
    assert {use.use_ref.material_type for use in result.materials} == {"IMAGE"}


@pytest.mark.parametrize("revoke_during_http", [False, True])
@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_balance_missing_tail_and_persistence_keep_database_work_batched(
    database_engine, redis_client, gateway_case, gateway_wire, revoke_during_http
):
    """末页缺口也须保留观测，但不能为每个缺口再开独立验权事务。"""
    from datetime import UTC, datetime

    from sqlalchemy import event

    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.ads import balances as balance_store

    context, route, original = gateway_case
    authenticated_subject(database_engine, gateway_case)
    advertisers = [original]
    with Session(database_engine) as session:
        for index in range(120):
            adv = f"missing-balance-{index}"
            advertisers.append(adv)
            session.add(
                AdvertiserAccount(
                    tenant_id=context.tenant_id,
                    advertiser_id=adv,
                    currency="USD",
                    timezone="UTC",
                )
            )
            session.add(
                BCAccountAccess(
                    tenant_id=context.tenant_id,
                    bc_id=route.bc_id,
                    advertiser_id=adv,
                    connection_id=route.connection_id,
                    in_bc=True,
                    active=True,
                    authorized=True,
                    checked_at=datetime.now(UTC),
                )
            )
        session.commit()
    member = {
        "user_id": "subject-1",
        "relation_status": "BOUND",
        "ext_user_role": {"finance_role": "ANALYST"},
    }
    row = {
        "advertiser_id": original,
        "currency": "USD",
        "balance_info": {"account_balance": "12.5"},
    }
    if revoke_during_http:
        calls = []

        def revoke_after_request():
            calls.append(True)
            if len(calls) == 2:
                with Session(database_engine) as session:
                    grant = session.get(
                        BCAccountAccess,
                        (
                            context.tenant_id,
                            route.bc_id,
                            advertisers[-1],
                            route.connection_id,
                        ),
                    )
                    grant.authorized = False
                    session.commit()

        gateway_wire["before"]["callback"] = revoke_after_request
    replies(
        gateway_wire,
        "OFFICIAL_API",
        {
            "bc_member_get": [page([member], size=20)],
            "advertiser_balance_get": [
                {
                    "advertiser_account_list": [row],
                    "page_info": page([row], size=100)["page_info"],
                }
            ],
        },
    )
    statements = []

    def capture(_connection, _cursor, statement, *_):
        statements.append(statement)

    event.listen(database_engine, "before_cursor_execute", capture)
    try:
        if revoke_during_http:
            with pytest.raises(DomainError) as failure:
                with gateway(database_engine, redis_client, gateway_case) as client:
                    client.ads.read_balance_page(tuple(advertisers))
            assert failure.value.code == "account_access_denied"
            return
        with gateway(database_engine, redis_client, gateway_case) as client:
            values, next_page = client.ads.read_balance_page(tuple(advertisers))
        assert next_page is None and len(values) == 121
        assert values[original].amount == Decimal("12.5")
        assert all(
            values[adv].availability == "BALANCE_MISSING" for adv in advertisers[1:]
        )
        with Session(database_engine) as session:
            balance_store.persist_balances(
                session, context=context, route=route, balances=values
            )
            session.rollback()
    finally:
        event.remove(database_engine, "before_cursor_execute", capture)
    assert len(statements) < 180
