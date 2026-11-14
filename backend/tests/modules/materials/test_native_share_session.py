"""原生共享使用真实 PG/Redis、官方客户端和本地 HTTP 协议替身。"""

import json
from time import monotonic
from uuid import uuid4

import pytest
from redis import Redis
from sqlalchemy import delete
from sqlmodel import Session, SQLModel

from app.core.config import settings
from app.integrations.tiktok.admission import PROTOCOL_OPERATIONS
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.jobs.admission import (
    admission_keys,
    admission_policy,
    admit_call,
    release_call,
)
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from tests.integrations.tiktok.gateway_support import (
    business_calls,
)
from tests.integrations.tiktok.gateway_support import (
    database_engine as database_engine,
)
from tests.integrations.tiktok.gateway_support import (
    gateway_case as gateway_case,
)
from tests.integrations.tiktok.gateway_support import (
    gateway_wire as gateway_wire,
)
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.accounts.test_material_gateway import after_material_http
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import asset, target


@pytest.fixture
def share_case(gateway_case, database_engine, monkeypatch):
    context, route, source = gateway_case
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **settings.TIKTOK_CALL_POLICIES,
            "endpoints": {
                operation: {"lease_ms": 970000}
                for operation in {*PROTOCOL_OPERATIONS, "materials.share_assets"}
            },
        },
    )
    with Session(database_engine) as db, db.begin():
        material = MaterialFile(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            file_name="shared.mp4",
            object_key=f"synthetic/{uuid4()}",
            byte_size=120,
            video_md5="a" * 32,
            storage_state="unavailable",
        )
        db.add(material)
        db.flush()
        env = {
            "context": context,
            "connection_id": route.connection_id,
            "bc_id": route.bc_id,
            "material_id": material.id,
            "source": source,
        }
        env["target"] = target(db, env, advertiser_id="90071992547409932")
        asset(db, env, source)
    try:
        yield env
    finally:
        with Session(database_engine) as db, db.begin():
            # 此 fixture 独占合成租户；按外键顺序清理，外层 gateway fixture 可重复清理。
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    db.execute(
                        delete(table).where(table.c.tenant_id == context.tenant_id)
                    )


@pytest.fixture
def share_wire(share_case, gateway_wire):
    data = {
        "list": [
            {
                "video_id": f"vid-{share_case['source']}",
                "material_id": "1234567890123456789",
                "file_name": "shared.mp4",
                "signature": "a" * 32,
                "displayable": True,
            }
        ]
    }
    tools = {c.operation: c.tool_name for c in load_tool_contracts()}
    gateway_wire["sdk_data"]["data"] = data
    gateway_wire["wire"].results[tools["materials.get_videos"]].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )
    gateway_wire["wire"].results[tools["materials.share_assets"]].append(
        {"content": [], "structuredContent": {"code": 0, "data": {}}}
    )
    return gateway_wire, tools


def test_native_share_reuses_one_mcp_handshake_and_catalog(
    share_case, share_wire, redis_client, monkeypatch
):
    from app.integrations.tiktok.mcp import transport

    wire, tools = share_wire
    # 在真实 HTTP 发送边界检查两次业务请求各自持有 Redis 租约，读取时没有 arm。
    original = transport._new_http_transport
    observed = []

    class AdmissionBoundary(original):
        async def handle_async_request(self, request):
            message = json.loads(request.content) if request.method == "POST" else {}
            if message.get("method") == "tools/call":
                operation = next(
                    name
                    for name, tool in tools.items()
                    if tool == message["params"]["name"]
                )
                keys = admission_keys(
                    f"official-mcp:{settings.MCP_SERVICE_QUOTA_SCOPE}",
                    operation,
                    share_case["context"].tenant_id,
                    share_case["source"],
                )
                assert redis_client.zcard(keys[3]) == 1
                armed = state(prepared.task_id)[1].remote_response.get(
                    "send_armed", False
                )
                observed.append((operation, armed))
            return await super().handle_async_request(request)

    monkeypatch.setattr(transport, "_new_http_transport", AdmissionBoundary)
    prepared = queue(share_case, share_case["target"])
    run(share_case, redis_client, prepared.task_id, kind="prepare")
    dist, operation, mapping = state(prepared.task_id)
    assert dist.status == "verifying", operation.remote_response
    assert mapping is None
    calls = business_calls(wire, "OFFICIAL_MCP")
    assert [call["params"]["name"] for call in calls] == [
        tools["materials.get_videos"],
        tools["materials.share_assets"],
    ]
    assert calls[1]["params"]["arguments"] == {
        "advertiser_id": share_case["source"],
        "asset_type": "VIDEO",
        "material_ids": ["1234567890123456789"],
        "shared_advertiser_ids": [share_case["target"]],
    }
    methods = [call["method"] for call in wire["wire"].calls]
    assert methods.count("initialize") == 1
    assert methods.count("tools/list") == 1
    assert calls[0]["session"] == calls[1]["session"]
    assert observed == [
        ("materials.get_videos", False),
        ("materials.share_assets", True),
    ]


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("change", ["source_upload", "target_upload", "claim"])
def test_native_share_rechecks_owner_and_permissions_after_read(
    share_case,
    share_wire,
    gateway_case,
    redis_client,
    database_engine,
    monkeypatch,
    change,
):
    wire, _ = share_wire
    prepared = queue(share_case, share_case["target"])
    replacement = uuid4()

    def mutate():
        with Session(database_engine) as db, db.begin():
            if change == "claim":
                db.get(
                    MaterialAssetOperation, state(prepared.task_id)[1].id
                ).attempt_token = replacement
            else:
                account = share_case[
                    "source" if change == "source_upload" else "target"
                ]
                db.get(
                    BCAccountAccess,
                    (
                        share_case["context"].tenant_id,
                        share_case["bc_id"],
                        account,
                        share_case["connection_id"],
                    ),
                ).can_upload = False

    after_material_http(monkeypatch, gateway_case[1].channel, mutate)
    run(share_case, redis_client, prepared.task_id, kind="prepare")
    _, operation, mapping = state(prepared.task_id)
    assert len(business_calls(wire, gateway_case[1].channel)) == 1
    assert not operation.remote_response.get("send_armed")
    assert mapping is None
    if change == "claim":
        assert operation.attempt_token == replacement


def test_shared_session_obeys_separate_share_admission(
    share_case, share_wire, redis_client, monkeypatch
):
    wire, _ = share_wire
    prepared = queue(share_case, share_case["target"])
    lease = uuid4()
    scope = {
        "app_scope": f"official-mcp:{settings.MCP_SERVICE_QUOTA_SCOPE}",
        "endpoint": "materials.share_assets",
        "tenant_id": share_case["context"].tenant_id,
        "advertiser_id": share_case["source"],
        "lease_id": lease,
    }
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **settings.TIKTOK_CALL_POLICIES,
            "endpoints": {
                **settings.TIKTOK_CALL_POLICIES["endpoints"],
                "materials.share_assets": {
                    "lease_ms": 970000,
                    "endpoint_max_inflight": 1,
                },
            },
        },
    )
    assert admit_call(
        redis_client, **scope, policy=admission_policy("materials.share_assets")
    ).granted
    try:
        run(share_case, redis_client, prepared.task_id, kind="prepare")
        assert len(business_calls(wire, "OFFICIAL_MCP")) == 1
        assert not state(prepared.task_id)[1].remote_response.get("send_armed")
    finally:
        release_call(redis_client, **scope)


def test_unknown_shared_session_cannot_reshare_on_prepare_retry(
    share_case, share_wire, redis_client
):
    wire, tools = share_wire
    prepared = queue(share_case, share_case["target"])
    wire["wire"].disconnect_after_accept(tools["materials.share_assets"])
    run(share_case, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "result_unknown"
    assert state(prepared.task_id)[1].remote_response["send_armed"]
    run(share_case, redis_client, prepared.task_id, kind="prepare")
    calls = business_calls(wire, "OFFICIAL_MCP")
    assert len(calls) == 2
    assert (
        sum(call["params"]["name"] == tools["materials.share_assets"] for call in calls)
        == 1
    )


def test_native_session_keeps_bounded_read_deadline_before_arm(
    share_case, share_wire, redis_client, monkeypatch
):
    from app.modules.materials import distribution

    wire, _ = share_wire
    prepared = queue(share_case, share_case["target"])
    monkeypatch.setattr(distribution, "READ_HARD_LIMIT", 11)
    wire["wire"].handshake_delay = 10
    start = monotonic()
    run(share_case, redis_client, prepared.task_id, kind="prepare")
    assert monotonic() - start < 8
    assert not business_calls(wire, "OFFICIAL_MCP")
    assert not state(prepared.task_id)[1].remote_response.get("send_armed")


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_new_owner_after_share_admission_stops_exact_physical_send(
    share_case, share_wire, gateway_case, redis_client, database_engine, monkeypatch
):
    from app.jobs.admission import _ADMISSION_SCRIPT

    wire, _ = share_wire
    prepared = queue(share_case, share_case["target"])
    original = Redis.eval
    replacement = uuid4()
    operation_id = state(prepared.task_id)[1].id
    channel = gateway_case[1].channel
    from app.integrations.tiktok.platform_quotas import sdk_endpoint

    endpoint = (
        sdk_endpoint("materials.share_assets")
        if channel == "OFFICIAL_API"
        else "materials.share_assets"
    )
    keys = admission_keys(
        f"official-mcp:{settings.MCP_SERVICE_QUOTA_SCOPE}"
        if channel == "OFFICIAL_MCP"
        else settings.TIKTOK_APP_ID,
        endpoint,
        share_case["context"].tenant_id,
        share_case["source"],
        rate_group=endpoint if channel == "OFFICIAL_API" else None,
    )
    replaced = []

    def eval_boundary(client, script, numkeys, *args):
        result = original(client, script, numkeys, *args)
        # 以真实准入脚本及共享桶识别发送边界，观测键增加不应让所有权栅栏测试失效。
        if (
            script == _ADMISSION_SCRIPT
            and list(args[: len(keys)]) == keys
            and result[0] == 1
        ):
            with Session(database_engine) as db, db.begin():
                operation = db.get(MaterialAssetOperation, operation_id)
                assert operation.remote_response["send_armed"]
                operation.attempt_token = replacement
                replaced.append(True)
        return result

    monkeypatch.setattr(Redis, "eval", eval_boundary)
    run(share_case, redis_client, prepared.task_id, kind="prepare")
    assert replaced == [True]
    assert len(business_calls(wire, channel)) == 1
    assert state(prepared.task_id)[1].attempt_token == replacement


def test_native_gateway_is_not_shared_by_independent_distribution_tasks(
    share_case, share_wire, redis_client, database_engine
):
    wire, tools = share_wire
    with Session(database_engine) as db, db.begin():
        second_target = target(db, share_case, advertiser_id="90071992547409933")
    first = queue(share_case, share_case["target"])
    source_result = wire["wire"].results[tools["materials.get_videos"]][0]
    wire["wire"].results[tools["materials.get_videos"]].append(source_result)
    run(share_case, redis_client, first.task_id, kind="prepare")
    # 第一批完成后到达的任务必须开启独立会话；同时已排队的相同范围任务现在会合批。
    second = queue(share_case, second_target)
    run(share_case, redis_client, second.task_id, kind="prepare")
    assert (
        state(first.task_id)[0].status == state(second.task_id)[0].status == "verifying"
    )
    calls = business_calls(wire, "OFFICIAL_MCP")
    assert len(calls) == 4
    assert calls[0]["session"] == calls[1]["session"]
    assert calls[2]["session"] == calls[3]["session"]
    assert calls[0]["session"] != calls[2]["session"]
