from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from mcp.types import CallToolResult
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.integrations.tiktok.mcp_auth.bootstrap import (
    candidate_business_centers,
    open_candidate_accounts,
)
from app.jobs.models import PendingDispatch
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionToolObservation,
    McpAuthorizationAttempt,
)
from app.modules.accounts.connections import (
    _publish_authorization,
    disable_connection,
    request_mcp_revocation,
)
from app.modules.accounts.models import DiscoveryRun, TenantBC, TikTokConnection
from tests.integrations.tiktok.mcp_wire import McpWire
from tests.modules.accounts.test_mcp_authorization import (  # noqa: F401
    accept,
    app_config,
    issue,
)
from tests.modules.accounts.test_mcp_authorization import (
    committed_context as _committed_context,
)
from tests.modules.accounts.test_mcp_authorization import (
    oauth_wire as _oauth_wire,
)


@pytest.fixture
def committed_context():
    parent = _committed_context.__wrapped__()
    base_committed_context = next(parent)
    yield base_committed_context
    from sqlmodel import delete

    from app.modules.accounts.connection_models import (
        ConnectionAuthorization,
        McpRefreshAttempt,
    )
    from app.modules.accounts.models import BCAccountAccess

    with Session(engine) as own:
        for model in (BCAccountAccess, ConnectionAuthorization, McpRefreshAttempt):
            own.exec(
                delete(model).where(model.tenant_id == base_committed_context.tenant_id)
            )
        own.commit()
    parent.close()


def bind_candidate_bc(session, *, context, attempt_id, bc_id):
    """单 BC 断言使用同一批量授权发布边界，不保留生产单候选生命周期。"""
    _, runs = _publish_authorization(
        session, context=context, attempt_id=attempt_id, selected=[bc_id]
    )
    return UUID(next(run["discovery_run_id"] for run in runs if run["bc_id"] == bc_id))


oauth_wire = _oauth_wire


@pytest.fixture
def candidate(committed_context, oauth_wire):
    state, identity = issue(committed_context)
    assert accept(state) == identity
    assert len(oauth_wire.calls) == 1
    return identity


@pytest.fixture
def catalog_wire(monkeypatch, policy, redis_client):
    from uuid import uuid4

    from app.integrations.tiktok.mcp import transport

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    monkeypatch.setattr(
        settings, "MCP_SERVICE_QUOTA_SCOPE", f"synthetic-test-{uuid4()}"
    )
    tools = [
        {
            "name": c.tool_name,
            "inputSchema": c.input_schema,
            "description": "synthetic-sensitive-description https://signed.invalid/private",
        }
        for c in load_tool_contracts()
    ]
    wire = McpWire(tools)
    # 与共享 gateway 夹具一致，避免真实数据库鉴权期间撞上本地服务一秒空闲断连。
    # 只关闭 HTTP keepalive；MCP session、业务总期限和发送前校验均保持不变。
    wire.close_connections = True
    wire.request_timeout = 30

    class LocalTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            assert request.url.host == "business-api.tiktok.com"
            request.url = httpx2.URL(wire.url)
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)
    yield wire
    wire.close()
    from hashlib import sha256

    digest = sha256(
        f"official-mcp:{settings.MCP_SERVICE_QUOTA_SCOPE}".encode()
    ).hexdigest()
    keys = list(redis_client.scan_iter(match=f"tiktok:{{{digest}}}:*"))
    if keys:
        redis_client.delete(*keys)


def bc_page(wire, *, page=1, total_pages=1, bcs=("bc-1", "bc-2"), total_number=2):
    if page == 1:
        wire.enqueue_result(
            "user_info_get",
            CallToolResult(
                content=[],
                structuredContent={
                    "code": 0,
                    "data": {"core_user_id": "synthetic-subject"},
                },
            ),
        )
    wire.enqueue_result(
        "bc_get",
        CallToolResult(
            content=[],
            structuredContent={
                "code": 0,
                "data": {
                    "list": [{"bc_info": {"bc_id": bc, "name": bc}} for bc in bcs],
                    "page_info": {
                        "page": page,
                        "page_size": 50,
                        "total_page": total_pages,
                        "total_number": total_number,
                    },
                },
            },
        ),
    )


def read_bcs(context, candidate, redis_client):
    return candidate_business_centers(
        database_engine=engine,
        redis_client=redis_client,
        context=context,
        attempt_id=candidate,
        task_deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def test_complete_catalog_and_bc_selection_enqueue_only(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    assert [
        item["bc_id"] for item in read_bcs(committed_context, candidate, redis_client)
    ] == ["bc-1", "bc-2"]
    bc_calls = [
        call["params"]
        for call in catalog_wire.calls
        if call["method"] == "tools/call" and call["params"]["name"] == "bc_get"
    ]
    assert bc_calls[0]["arguments"] == {"page": 1, "page_size": 50}
    with Session(engine) as own:
        observation = own.exec(
            select(ConnectionToolObservation).where(
                ConnectionToolObservation.candidate_attempt_id == candidate
            )
        ).one()
        assert observation.pagination_complete
        assert "synthetic-sensitive-description" not in str(observation.tool_schemas)
        run_id = bind_candidate_bc(
            own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
        )
        own.commit()
        run = own.get(DiscoveryRun, run_id)
        attempt = own.get(McpAuthorizationAttempt, candidate)
        assert run.mcp_candidate_attempt_id is None
        assert run.authorization_revision == 1
        assert run.work["bc_id"] == "bc-1"
        active_observation = own.get(
            ConnectionToolObservation, UUID(run.work["observation_id"])
        )
        assert active_observation.candidate_attempt_id is None
        assert own.get(TikTokConnection, attempt.connection_id).status == "ACTIVE"
        assert attempt.status == "ACCEPTED" and attempt.candidate_ciphertext is None
        binding = own.get(
            BCConnectionBinding,
            (committed_context.tenant_id, "bc-1", attempt.connection_id),
        )
        assert binding.status == "SYNCING" and binding.authorization_revision == 1
        assert (
            own.exec(
                select(BCDefaultRoute).where(
                    BCDefaultRoute.tenant_id == committed_context.tenant_id
                )
            ).all()
            == []
        )
        dispatch = own.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == committed_context.tenant_id
            )
        ).one()
        assert dispatch.task_name == "accounts.mcp_discover"
        assert dispatch.payload == {"run_id": str(run_id), "revision": 0}
        assert (
            bind_candidate_bc(
                own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
            )
            == run_id
        )
        with pytest.raises(DomainError) as error:
            bind_candidate_bc(
                own, context=committed_context, attempt_id=candidate, bc_id="bc-2"
            )
        assert error.value.code == "mcp_candidate_superseded"
    methods = [c["method"] for c in catalog_wire.calls]
    assert methods.index("tools/list") < methods.index("tools/call")


def test_unknown_bc_and_partial_pagination_do_not_replace_active(
    committed_context, candidate, catalog_wire, redis_client
):
    with Session(engine) as own:
        attempt = own.get(McpAuthorizationAttempt, candidate)
        connection_id = attempt.connection_id
        connection = own.get(TikTokConnection, connection_id)
        connection.status = "ACTIVE"
        connection.credential_ciphertext = "synthetic-old-ciphertext"
        own.add(connection)
        own.commit()
    bc_page(
        catalog_wire,
        total_pages=2,
        bcs=tuple(f"bc-{i}" for i in range(50)),
        total_number=51,
    )
    catalog_wire.enqueue_result(
        "bc_get",
        CallToolResult(
            content=[],
            structuredContent={"code": 40000, "message": "synthetic-private-error"},
        ),
    )
    with pytest.raises(DomainError):
        read_bcs(committed_context, candidate, redis_client)
    with Session(engine) as own:
        with pytest.raises(DomainError) as error:
            bind_candidate_bc(
                own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
            )
        assert error.value.code == "mcp_candidate_directory_incomplete"
        assert (
            own.exec(
                select(DiscoveryRun).where(
                    DiscoveryRun.tenant_id == committed_context.tenant_id
                )
            ).all()
            == []
        )
        connection = own.get(TikTokConnection, connection_id)
        assert connection.status == "ACTIVE"
        assert connection.credential_ciphertext == "synthetic-old-ciphertext"
    bc_page(catalog_wire)
    read_bcs(committed_context, candidate, redis_client)
    with Session(engine) as own:
        with pytest.raises(DomainError) as error:
            bind_candidate_bc(
                own,
                context=committed_context,
                attempt_id=candidate,
                bc_id="bc-not-visible",
            )
        assert error.value.code == "mcp_candidate_bc_unknown"


def test_candidate_cross_tenant_forbidden_before_http(
    session, candidate, catalog_wire, redis_client
):
    from tests.modules.conftest import create_context

    other = create_context(session, role="tenant_admin")
    with pytest.raises(DomainError):
        read_bcs(other, candidate, redis_client)
    assert catalog_wire.calls == []


def test_candidate_admin_rechecked_between_calls(
    committed_context, candidate, catalog_wire, redis_client
):
    from app.modules.tenants.models import TenantMembership

    bc_page(catalog_wire)
    with open_candidate_accounts(
        database_engine=engine,
        redis_client=redis_client,
        context=committed_context,
        attempt_id=candidate,
        task_deadline=datetime.now(UTC) + timedelta(seconds=30),
    ) as gateway:
        assert gateway.business_centers(page=1, page_size=50).items
        with Session(engine) as own:
            member = own.get(
                TenantMembership,
                (committed_context.tenant_id, committed_context.actor_id),
            )
            member.role = "operator"
            own.add(member)
            own.commit()
        with pytest.raises(DomainError) as error:
            gateway.business_centers(page=1, page_size=50)
        assert error.value.code == "action_forbidden"
    assert sum(c["method"] == "tools/call" for c in catalog_wire.calls) == 1


def test_incomplete_catalog_never_calls_business(
    committed_context, candidate, catalog_wire, redis_client
):
    catalog_wire.tools = catalog_wire.tools[1:]
    with pytest.raises(DomainError):
        read_bcs(committed_context, candidate, redis_client)
    assert not any(c["method"] == "tools/call" for c in catalog_wire.calls)
    with Session(engine) as own:
        assert (
            own.exec(
                select(ConnectionToolObservation).where(
                    ConnectionToolObservation.candidate_attempt_id == candidate
                )
            ).all()
            == []
        )


def test_existing_binding_reauthorizes_all_visible_bcs(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    read_bcs(committed_context, candidate, redis_client)
    with Session(engine) as own:
        attempt = own.get(McpAuthorizationAttempt, candidate)
        own.add(TenantBC(tenant_id=committed_context.tenant_id, bc_id="bc-1"))
        own.flush()
        own.add(
            BCConnectionBinding(
                tenant_id=committed_context.tenant_id,
                bc_id="bc-1",
                connection_id=attempt.connection_id,
                kind="OFFICIAL_MCP",
            )
        )
        own.flush()
        connection_id, runs = _publish_authorization(
            own, context=committed_context, attempt_id=candidate, selected=["bc-2"]
        )
        assert {run["bc_id"] for run in runs} == {"bc-1", "bc-2"}
        assert own.get(TikTokConnection, connection_id).authorization_revision == 1
        own.rollback()


def test_disable_cancels_unsent_dispatch_and_preserves_history(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    read_bcs(committed_context, candidate, redis_client)
    with Session(engine) as own:
        run_id = bind_candidate_bc(
            own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
        )
        attempt = own.get(McpAuthorizationAttempt, candidate)
        connection_id = attempt.connection_id
        run = own.get(DiscoveryRun, run_id)
        run.sent_count = 1
        own.add(run)
        now = datetime.now(UTC)
        inactive_ids = set()
        for state in ("superseded", "retired"):
            inactive = PendingDispatch(
                tenant_id=committed_context.tenant_id,
                actor_id=committed_context.actor_id,
                task_name="accounts.discover",
                task_key=f"inactive-discovery:{uuid4()}",
                payload={"run_id": str(run_id)},
            )
            if state == "superseded":
                inactive.superseded_at = now
            else:
                inactive.dispatch_contract_version = 2
                inactive.issued_at = now - timedelta(hours=1)
                inactive.valid_until = now + timedelta(hours=1)
                inactive.retired_at = now
            inactive_ids.add(inactive.id)
            own.add(inactive)
        own.commit()
        disable_connection(
            own,
            context=committed_context,
            connection_id=connection_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=5),
        )
        own.commit()
        assert own.get(TikTokConnection, connection_id).status == "DISABLED"
        run = own.get(DiscoveryRun, run_id)
        assert run.status == "CANCELLED" and run.sent_count == 1
        assert (
            set(
                own.exec(
                    select(PendingDispatch.id).where(
                        PendingDispatch.tenant_id == committed_context.tenant_id
                    )
                ).all()
            )
            == inactive_ids
        )
        with pytest.raises(DomainError) as error:
            request_mcp_revocation(
                own, context=committed_context, connection_id=connection_id
            )
        assert error.value.code == "mcp_revocation_unsupported"
    before = len(catalog_wire.calls)
    with pytest.raises(DomainError):
        read_bcs(committed_context, candidate, redis_client)
    assert len(catalog_wire.calls) == before


def test_complete_bc_pagination_uses_same_candidate_and_stable_totals(
    committed_context, candidate, catalog_wire, redis_client
):
    first_ids = tuple(f"bc-{i}" for i in range(50))
    bc_page(catalog_wire, total_pages=3, bcs=first_ids, total_number=101)
    bc_page(
        catalog_wire,
        page=2,
        total_pages=3,
        bcs=tuple(f"bc-{i}" for i in range(50, 100)),
        total_number=101,
    )
    bc_page(catalog_wire, page=3, total_pages=3, bcs=("bc-last",), total_number=101)
    items = read_bcs(committed_context, candidate, redis_client)
    assert len(items) == 101
    with Session(engine) as own:
        run_id = bind_candidate_bc(
            own, context=committed_context, attempt_id=candidate, bc_id="bc-last"
        )
        assert own.get(DiscoveryRun, run_id).work["bc_id"] == "bc-last"
        own.rollback()
    requests = [
        call
        for call in catalog_wire.calls
        if call["method"] == "tools/call" and call["params"]["name"] == "bc_get"
    ]
    assert [call["params"]["arguments"]["page"] for call in requests] == [1, 2, 3]
    assert all(call["params"]["arguments"]["page_size"] == 50 for call in requests)


@pytest.mark.parametrize(
    "operation", ["build.create_campaign", "materials.upload_video_url"]
)
def test_optional_write_contract_drift_does_not_block_bc_candidate(
    committed_context, candidate, catalog_wire, redis_client, operation
):
    contract = next(c for c in load_tool_contracts() if c.operation == operation)
    target = next(
        tool for tool in catalog_wire.tools if tool["name"] == contract.tool_name
    )
    target["inputSchema"] = {
        "type": "object",
        "required": ["new_remote_field"],
        "properties": {"new_remote_field": {"type": "string"}},
    }
    bc_page(catalog_wire)
    assert read_bcs(committed_context, candidate, redis_client)
    with Session(engine) as own:
        observation = own.exec(
            select(ConnectionToolObservation).where(
                ConnectionToolObservation.candidate_attempt_id == candidate
            )
        ).one()
        assert observation.pagination_complete
        assert contract.tool_name not in observation.tool_schemas
        assert (
            observation.call_evidence["unavailable_tools"][contract.tool_name]
            == "mcp_contract_changed"
        )
        run_id = bind_candidate_bc(
            own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
        )
        assert own.get(DiscoveryRun, run_id).work["bc_id"] == "bc-1"
        own.rollback()


def test_mandatory_account_contract_drift_blocks_business(
    committed_context, candidate, catalog_wire, redis_client
):
    target = next(tool for tool in catalog_wire.tools if tool["name"] == "bc_get")
    target["inputSchema"] = {
        "type": "object",
        "properties": {"changed": {"type": "string"}},
    }
    with pytest.raises(DomainError) as error:
        read_bcs(committed_context, candidate, redis_client)
    assert error.value.code == "mcp_contract_changed"
    assert not any(c["method"] == "tools/call" for c in catalog_wire.calls)


def test_membership_rechecked_before_next_call_and_binding(
    committed_context, candidate, catalog_wire, redis_client
):
    from app.models import User
    from app.modules.tenants.models import TenantMembership

    with Session(engine) as own:
        user = own.get(User, committed_context.actor_id)
        user.is_superuser = False
        own.add(user)
        own.commit()
    bc_page(catalog_wire)
    read_bcs(committed_context, candidate, redis_client)
    with open_candidate_accounts(
        database_engine=engine,
        redis_client=redis_client,
        context=committed_context,
        attempt_id=candidate,
        task_deadline=datetime.now(UTC) + timedelta(seconds=30),
    ) as gateway:
        with Session(engine) as own:
            member = own.get(
                TenantMembership,
                (committed_context.tenant_id, committed_context.actor_id),
            )
            member.active = False
            own.add(member)
            own.commit()
        before = len(catalog_wire.calls)
        with pytest.raises(DomainError) as error:
            gateway.authorization_facts()
        assert error.value.code == "tenant_forbidden"
        with pytest.raises(DomainError) as error:
            gateway.business_centers(page=1, page_size=50)
        assert error.value.code == "tenant_forbidden"
        assert len(catalog_wire.calls) == before
    with Session(engine) as own:
        with pytest.raises(DomainError) as error:
            bind_candidate_bc(
                own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
            )
        assert error.value.code == "tenant_forbidden"


def test_non_admin_without_membership_cannot_read_another_admin_candidate(
    committed_context, candidate, catalog_wire, redis_client
):
    from uuid import uuid4

    from sqlmodel import delete

    from app.core.context import TenantContext
    from app.models import User

    other = User(username=str(uuid4()), hashed_password="unused", is_superuser=False)
    actor_id = other.id
    with Session(engine) as own:
        own.add(other)
        own.commit()
    context = TenantContext(
        tenant_id=committed_context.tenant_id, actor_id=actor_id, role="platform_admin"
    )
    try:
        with pytest.raises(DomainError) as error:
            read_bcs(context, candidate, redis_client)
        assert error.value.code == "tenant_forbidden"
        assert catalog_wire.calls == []
    finally:
        with Session(engine) as own:
            own.exec(delete(User).where(User.id == actor_id))
            own.commit()


def test_manifest_change_reobserves_candidate_before_binding(
    committed_context, candidate, catalog_wire, redis_client
):
    from app.integrations.tiktok.mcp.protocol import load_mcp_protocol

    bc_page(catalog_wire)
    read_bcs(committed_context, candidate, redis_client)
    with Session(engine) as own:
        previous = own.exec(
            select(ConnectionToolObservation).where(
                ConnectionToolObservation.candidate_attempt_id == candidate
            )
        ).one()
        old_id = previous.id
        previous.expected_contract_revision = "0" * 64
        own.add(previous)
        own.commit()
        with pytest.raises(DomainError) as error:
            bind_candidate_bc(
                own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
            )
        assert error.value.code == "mcp_candidate_directory_incomplete"
    before = sum(call["method"] == "tools/list" for call in catalog_wire.calls)
    bc_page(catalog_wire)
    read_bcs(committed_context, candidate, redis_client)
    assert sum(call["method"] == "tools/list" for call in catalog_wire.calls) > before
    with Session(engine) as own:
        run_id = bind_candidate_bc(
            own, context=committed_context, attempt_id=candidate, bc_id="bc-1"
        )
        run = own.get(DiscoveryRun, run_id)
        observation = own.get(ConnectionToolObservation, run.work["observation_id"])
        assert observation.id != old_id
        assert (
            observation.expected_contract_revision
            == load_mcp_protocol().schema_manifest_sha256
        )
