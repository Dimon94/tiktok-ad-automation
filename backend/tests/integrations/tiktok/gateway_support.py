"""共享实际 gateway/PG/Redis 与 SDK/MCP HTTP fixture，不依赖测试模块。"""

import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx2
import pytest
from sqlmodel import Session, delete, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol, load_tool_contracts
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.models import User
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
    ConnectionToolObservation,
    McpRefreshAttempt,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import freeze_route
from app.modules.tenants.models import Tenant, TenantMembership
from tests.integrations.tiktok.mcp_wire import McpWire
from tests.modules.conftest import create_context


def retains_build_history(request, database_engine):
    """只有本次独占临时库可保留不可变历史，交由其 fixture 删除整个库。"""
    try:
        retained = request.getfixturevalue("retain_build_history")
    except pytest.FixtureLookupError:
        return False
    if retained is False:
        return False
    assert retained is database_engine
    assert re.fullmatch(
        r"strategy_[0-9a-f]{32}_test", database_engine.url.database or ""
    )
    assert request.getfixturevalue("isolated_strategy_database")[0] is database_engine
    return True


@pytest.fixture
def database_engine():
    from app.core.db import engine
    from tests.database import require_test_database

    require_test_database(str(settings.DATABASE_URL))
    assert engine.dialect.name == "postgresql"
    return engine


@pytest.fixture
def gateway_case(request, database_engine, redis_client, monkeypatch, policy, tmp_path):
    retain_history = retains_build_history(request, database_engine)
    channel = getattr(request, "param", "OFFICIAL_MCP")
    profile = load_mcp_protocol()
    # 当前工厂核对真实本地注册client绑定；共享fixture必须独立提供合成部署材料。
    redirect = "https://tkada.example.test/api/integrations/tiktok/mcp/callback"
    registration = tmp_path / "gateway-registration.json"
    registration.write_text(
        json.dumps(
            {
                "client_id": "synthetic-client",
                "issuer": profile.issuer,
                "resource": profile.resource,
                "token_endpoint_auth_method": "none",
                "redirect_uris": [redirect],
            }
        )
    )
    monkeypatch.setattr(settings, "MCP_CLIENT_REGISTRATION_REF", str(registration))
    monkeypatch.setattr(settings, "MCP_REDIRECT_URI", redirect)
    monkeypatch.setattr(settings, "MCP_SERVICE_QUOTA_SCOPE", f"test-gateway-{uuid4()}")
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    schemas = {
        contract.tool_name: {
            "name": contract.tool_name,
            "inputSchema": contract.input_schema,
            **(
                {"outputSchema": contract.output_schema}
                if contract.output_schema
                else {}
            ),
        }
        for contract in load_tool_contracts()
    }
    with Session(database_engine) as session:
        context = create_context(session, role="tenant_admin")
        connection = TikTokConnection(
            tenant_id=context.tenant_id,
            kind=channel,
            status="ACTIVE",
            credential_revision=1,
            authorization_revision=1,
            service_profile=profile.revision if channel == "OFFICIAL_MCP" else None,
            adapter_contract_revision=profile.schema_manifest_sha256
            if channel == "OFFICIAL_MCP"
            else "official-api-v1",
            credential_ciphertext=encrypt_credentials(
                tenant_id=context.tenant_id,
                value={
                    "access_token": "synthetic-original-token",
                    "refresh_token": "synthetic-refresh",
                    "client_id": "synthetic-client",
                    "token_type": "Bearer",
                    "scopes": '["mcp:tt4b"]',
                    "issuer": profile.issuer,
                    "resource": profile.resource,
                    "received_at": datetime.now(UTC).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            ),
        )
        bc = TenantBC(tenant_id=context.tenant_id, bc_id="1234567890123456789")
        account = AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id="90071992547409931",
            name="Synthetic",
            currency="USD",
            timezone="UTC",
            remote_status="STATUS_ENABLE",
        )
        session.add_all([connection, bc, account])
        session.flush()
        grant = BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            advertiser_id=account.advertiser_id,
            connection_id=connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            can_upload=True,
            can_build=True,
            permission_state="VERIFIED",
            checked_at=datetime.now(UTC),
        )
        authorization = ConnectionAuthorization(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            authorization_revision=1,
            scopes=["mcp:tt4b"],
            source="SYNTHETIC_VERIFIED_EVIDENCE",
            issuer=profile.issuer
            if channel == "OFFICIAL_MCP"
            else "https://business-api.tiktok.com",
            resource=profile.resource
            if channel == "OFFICIAL_MCP"
            else "https://business-api.tiktok.com/open_api/v1.3",
            permission_summary={
                "read_authorized": True,
                "upload_authorized": True,
                "build_authorized": True,
            },
            verified_at=datetime.now(UTC),
        )
        session.add_all(
            [
                grant,
                authorization,
                BCConnectionBinding(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    connection_id=connection.id,
                    kind=channel,
                ),
                ConnectionToolObservation(
                    tenant_id=context.tenant_id,
                    connection_id=connection.id,
                    schema_digest="1" * 64,
                    expected_contract_revision=connection.adapter_contract_revision,
                    pagination_complete=True,
                    tool_schemas=schemas,
                ),
            ]
        )
        session.flush()
        session.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id, bc_id=bc.bc_id, connection_id=connection.id
            )
        )
        session.commit()
        route = freeze_route(session, context=context, bc_id=bc.bc_id)
        case = context, route, account.advertiser_id
    try:
        yield case
    finally:
        if not retain_history:
            from app.modules.sync_control.models import (
                SyncExecutionScope,
                SyncRequirement,
                SyncSchedule,
                SyncScope,
            )

            with Session(database_engine) as session:
                for model in (
                    SyncExecutionScope,
                    SyncSchedule,
                    SyncRequirement,
                    SyncScope,
                    PendingDispatch,
                    DispatchTenantCursor,
                    McpRefreshAttempt,
                    ConnectionToolObservation,
                    ConnectionAuthorization,
                    BCDefaultRoute,
                    BCConnectionBinding,
                    BCAccountAccess,
                    TikTokConnection,
                    AdvertiserAccount,
                    TenantBC,
                    TenantMembership,
                ):
                    session.exec(
                        delete(model).where(model.tenant_id == context.tenant_id)
                    )
                session.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
                session.exec(delete(User).where(User.id == context.actor_id))
                session.commit()
        for scope in (
            f"official-mcp:{settings.MCP_SERVICE_QUOTA_SCOPE}",
            settings.TIKTOK_APP_ID,
        ):
            digest = sha256(scope.encode()).hexdigest()
            keys = list(redis_client.scan_iter(f"tiktok:{{{digest}}}:*"))
            if keys:
                redis_client.delete(*keys)


@pytest.fixture
def gateway_wire(monkeypatch, gateway_case, database_engine):
    from app.integrations.tiktok.mcp import transport

    _, route, _ = gateway_case
    contracts = load_tool_contracts()
    wire = McpWire(
        [
            {"name": contract.tool_name, "inputSchema": contract.input_schema}
            for contract in contracts
        ]
    )
    # 本地服务的 1 秒空闲超时可能撞上连接池选中旧 TCP 后的调度间隙。
    # 显式关闭 HTTP 连接消除该竞争；MCP session 仍复用，专门传输测试保留 keepalive。
    wire.close_connections = True
    # 小内存 Linux 的同步鉴权会暂占 SDK 事件循环；已接受的协议连接不能
    # 被替身一秒请求读取期限提前切断。业务40秒总预算及真实发送门禁不变。
    wire.request_timeout = 30
    tokens = []
    before = {"callback": None}

    def check():
        # A different real Session can acquire the connection lock at HTTP entry.
        with Session(database_engine) as session:
            session.exec(
                select(TikTokConnection)
                .where(TikTokConnection.id == route.connection_id)
                .with_for_update(nowait=True)
            ).one()
        if before["callback"]:
            before["callback"]()

    class LocalTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            assert str(request.url) == load_mcp_protocol().endpoint
            check()
            tokens.append(request.headers.get("authorization"))
            request.url = httpx2.URL(wire.url)
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)
    calls = []
    sdk_data = {"data": {"vo_min_roas": "1.25"}}

    def sdk_request(_pool, method, url, **kwargs):
        check()
        # 此运输替身取代整个PoolManager.request，未进入真实TrackedConnection；
        # 在同一个合成HTTP发送点发出观察信号，不以业务返回页数伪造发送成本。
        cost = getattr(_pool, "_sync_attempt_observer", None)
        if cost is not None:
            observer, endpoint = cost
            observer.attempt(endpoint)
        calls.append((method, url, kwargs))
        tokens.append(kwargs.get("headers", {}).get("Access-Token"))
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": sdk_data["data"], "request_id": "synthetic-request"}
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", sdk_request)
    tool = next(
        contract.tool_name
        for contract in contracts
        if contract.operation == "scene.check_vbo"
    )
    for _ in range(5):
        wire.results[tool].append(
            {
                "content": [],
                "structuredContent": {"code": 0, "data": {"vo_min_roas": "1.25"}},
            }
        )
    result = {
        "wire": wire,
        "tokens": tokens,
        "sdk_calls": calls,
        "before": before,
        "vbo_tool": tool,
        "sdk_data": sdk_data,
    }
    try:
        yield result
    finally:
        wire.close()


def gateway(database_engine, redis_client, case):
    context, route, _ = case
    return open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=datetime.now(UTC) + timedelta(seconds=20),
    )


def read_vbo(client, case):
    return client.scenes.read_page(
        resource="vbo", advertiser_id=case[2], page=1, minis_id=None
    )


def business_calls(wire, channel):
    if channel == "OFFICIAL_API":
        return wire["sdk_calls"]
    return [call for call in wire["wire"].calls if call.get("method") == "tools/call"]
