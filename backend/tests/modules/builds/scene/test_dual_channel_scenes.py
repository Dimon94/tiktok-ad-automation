"""同一正式 SceneJob 通过真实 SDK/MCP HTTP 完成五类只读事实。"""

from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.modules.accounts.models import TikTokConnection
from app.modules.builds.scene import read_scene_context
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from tests.integrations.tiktok.gateway_support import business_calls
from tests.modules.builds.scene.support import enqueue, ensure, run, scene_responses


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_minis_catalog_resolves_link_without_full_scene_budget_evidence(
    database_engine, redis_client, scene_case, gateway_wire
):
    from sqlmodel import delete

    from app.modules.builds.mini_targets import MiniTarget
    from app.modules.providers.models import PromotionLink

    case = scene_case
    with Session(database_engine) as db, db.begin():
        db.exec(
            delete(MiniTarget).where(MiniTarget.tenant_id == case["context"].tenant_id)
        )
        db.get(
            PromotionLink, case["link_id"]
        ).url = "https://www.tiktok.com/minis/synthetic-minis"
    catalog = ensure(database_engine, case)
    for resource in ("minis", "identity", "cta", "vbo", "regions"):
        enqueue(gateway_wire, resource, scene_responses(case)[resource])
        job = run(database_engine, redis_client, case, catalog.job_id)
    assert job.status == "COMPLETE" and job.minis_id is None
    assert "budget" not in job.facts

    # 目录与账户事实提前准备，确认目标后全部复用，不再串行补读四类资料。
    full = ensure(database_engine, case)
    assert full.state == "ready" and full.job_id != catalog.job_id
    assert ensure(database_engine, case).job_id == full.job_id
    with Session(database_engine) as db:
        assert db.get(SceneJob, catalog.job_id).status == "COMPLETE"
        assert db.get(SceneJob, full.job_id).minis_id == "synthetic-minis"
    assert len(business_calls(gateway_wire, case["route"].channel)) == 5


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("change", ["viewer", "claim"])
def test_mcp_initialize_cannot_outlive_scene_worker_authority(
    database_engine, redis_client, scene_case, gateway_wire, monkeypatch, change
):
    import json

    from app.integrations.tiktok.mcp import transport
    from app.modules.tenants.models import TenantMembership

    case = scene_case
    receipt = ensure(database_engine, case)
    enqueue(gateway_wire, "identity", scene_responses(case)["identity"])
    original_transport = transport._new_http_transport
    replacement_claim = uuid4()

    class AfterInitialize(original_transport):
        async def handle_async_request(self, request):
            message = json.loads(request.content) if request.method == "POST" else {}
            response = await super().handle_async_request(request)
            if message.get("method") == "initialize":
                # 独立提交发生在握手完成、业务 HTTP 尚未发送的真实传输边界。
                with Session(database_engine) as db, db.begin():
                    if change == "viewer":
                        member = db.get(
                            TenantMembership,
                            (case["context"].tenant_id, case["context"].actor_id),
                        )
                        member.role = "viewer"
                    else:
                        db.get(SceneJob, receipt.job_id).claim_token = replacement_claim
            return response

    monkeypatch.setattr(transport, "_new_http_transport", AfterInitialize)
    job = run(database_engine, redis_client, case, receipt.job_id)
    assert not business_calls(gateway_wire, "OFFICIAL_MCP")
    with Session(database_engine) as db:
        assert not db.exec(
            select(SceneJobPage).where(SceneJobPage.job_id == job.id)
        ).all()
    if change == "viewer":
        assert (job.status, job.error_code) == ("BLOCKED", "action_forbidden")
    else:
        assert job.claim_token == replacement_claim


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize(
    ("refresh_status", "error_code"),
    [
        ("OUTCOME_UNKNOWN", "mcp_refresh_unknown"),
        ("REAUTH_REQUIRED", "mcp_refresh_reauth_required"),
    ],
)
def test_scene_preserves_terminal_refresh_state_without_business_http(
    database_engine, redis_client, scene_case, gateway_wire, refresh_status, error_code
):
    from datetime import UTC, datetime, timedelta

    from app.core.credentials import decrypt_credentials, encrypt_credentials
    from app.modules.accounts.connection_models import McpRefreshAttempt

    case = scene_case
    receipt = ensure(database_engine, case)
    with Session(database_engine) as db, db.begin():
        if refresh_status == "OUTCOME_UNKNOWN":
            db.add(
                McpRefreshAttempt(
                    tenant_id=case["context"].tenant_id,
                    connection_id=case["route"].connection_id,
                    base_credential_revision=1,
                    base_authorization_revision=1,
                    status=refresh_status,
                    request_armed_at=datetime.now(UTC),
                )
            )
        else:
            conn = db.get(TikTokConnection, case["route"].connection_id)
            credentials = decrypt_credentials(
                tenant_id=conn.tenant_id, ciphertext=conn.credential_ciphertext
            )
            credentials.pop("refresh_token", None)
            credentials["expires_at"] = (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
            conn.credential_ciphertext = encrypt_credentials(
                tenant_id=conn.tenant_id, value=credentials
            )
    job = run(database_engine, redis_client, case, receipt.job_id)
    assert (job.status, job.error_code) == ("BLOCKED", error_code)
    assert not business_calls(gateway_wire, "OFFICIAL_MCP")


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_one_frozen_job_completes_on_both_channels_without_api_app_for_mcp(
    database_engine,
    redis_client,
    scene_case,
    gateway_wire,
    monkeypatch,
):
    case = scene_case
    if case["route"].channel == "OFFICIAL_MCP":
        for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
            monkeypatch.setattr(settings, name, "")
    receipt = ensure(database_engine, case)
    assert receipt.state == "queued", receipt.reason_code
    for resource, data in scene_responses(case).items():
        enqueue(gateway_wire, resource, data)
        job = run(database_engine, redis_client, case, receipt.job_id)
        assert job.status == ("COMPLETE" if resource == "regions" else "PENDING"), (
            job.error_code
        )
    assert len(business_calls(gateway_wire, case["route"].channel)) == 5
    with Session(database_engine) as db:
        facts = read_scene_context(
            db,
            context=case["context"],
            bc_id=case["route"].bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=case["route"],
        )
        assert facts.supported, facts.reason_codes
        assert (
            facts.creative_fields["creative_info"]["identity_id"]
            == "synthetic-identity"
        )
        assert facts.adgroup_fields["targeting_spec"]["location_ids"] == ("6252001",)
        pages = db.exec(
            select(SceneJobPage).where(SceneJobPage.job_id == receipt.job_id)
        ).all()
        assert len(pages) == 5
        assert {page.resource for page in pages} == {
            "identity",
            "minis",
            "cta",
            "vbo",
            "regions",
        }
        assert all(page.source_revision and page.endpoint for page in pages)
        if case["route"].channel == "OFFICIAL_MCP":
            assert all(page.mcp_request_id for page in pages)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_selected_identity_is_validated_against_each_account_catalog(
    database_engine, redis_client, scene_case, gateway_wire
):
    case = scene_case
    receipt = ensure(database_engine, case)
    responses = scene_responses(case)
    responses["identity"]["identity_list"].append(
        {
            "identity_id": "second-identity",
            "identity_type": "TT_USER",
            "available_status": "AVAILABLE",
            "can_push_video": True,
            "is_gpppa": False,
            "display_name": "Same visible name",
            "username": "same_name",
        }
    )
    responses["identity"]["page_info"]["total_number"] = 2
    for resource, data in responses.items():
        enqueue(gateway_wire, resource, data)
        run(database_engine, redis_client, case, receipt.job_id)

    with Session(database_engine) as db:
        selected = read_scene_context(
            db,
            context=case["context"],
            bc_id=case["route"].bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=case["route"],
            selected_identity={
                "identity_id": "second-identity",
                "identity_type": "TT_USER",
            },
        )
        assert selected.supported, selected.reason_codes
        assert (
            selected.creative_fields["creative_info"]["identity_id"]
            == "second-identity"
        )
        unavailable = read_scene_context(
            db,
            context=case["context"],
            bc_id=case["route"].bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=case["route"],
            selected_identity={
                "identity_id": "not-authorized-here",
                "identity_type": "TT_USER",
            },
        )
        assert not unavailable.supported
        assert "identity_unavailable" in unavailable.reason_codes


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "change", ["credential", "authorization", "disabled", "read_fact"]
)
def test_http_receipt_publish_checks_current_semantics_but_accepts_credential_rotation(
    database_engine,
    redis_client,
    scene_case,
    gateway_wire,
    monkeypatch,
    change,
):
    from app.modules.accounts.connection_models import ConnectionAuthorization

    case = scene_case
    receipt = ensure(database_engine, case)
    enqueue(gateway_wire, "identity", scene_responses(case)["identity"])

    # 此测试经共享 HTTP 边界进入远端时完成独立事务变化。SDK与MCP均不mock gateway。
    def mutate():
        gateway_wire["before"]["callback"] = None
        with Session(database_engine) as db, db.begin():
            conn = db.get(TikTokConnection, case["route"].connection_id)
            if change == "credential":
                conn.credential_revision += 1
            elif change == "authorization":
                conn.authorization_revision += 1
            elif change == "disabled":
                conn.status = "DISABLED"
            else:
                auth = db.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id == conn.id
                    )
                ).one()
                auth.permission_summary = {
                    **auth.permission_summary,
                    "read_authorized": False,
                }

    # MCP握手也有物理HTTP，因此仅在tools/call已通过准入后触发。包装传输响应见下。
    if case["route"].channel == "OFFICIAL_API":
        import urllib3

        original = urllib3.PoolManager.request

        def after(pool, method, url, **kwargs):
            response = original(pool, method, url, **kwargs)
            mutate()
            return response

        monkeypatch.setattr(urllib3.PoolManager, "request", after)
    else:
        import json

        from app.integrations.tiktok.mcp import transport

        original_transport = transport._new_http_transport

        class AfterResponse(original_transport):
            async def handle_async_request(self, request):
                message = (
                    json.loads(request.content) if request.method == "POST" else {}
                )
                response = await super().handle_async_request(request)
                if message.get("method") == "tools/call":
                    mutate()
                return response

        monkeypatch.setattr(transport, "_new_http_transport", AfterResponse)
    job = run(database_engine, redis_client, case, receipt.job_id)
    with Session(database_engine) as db:
        pages = db.exec(select(SceneJobPage).where(SceneJobPage.job_id == job.id)).all()
    if change == "credential":
        assert job.status == "PENDING", job.error_code
        assert len(pages) == 1
    else:
        assert job.status in {"STALE", "BLOCKED"}, job.error_code
        assert not pages
    assert len(business_calls(gateway_wire, case["route"].channel)) == 1


def test_historical_job_without_route_is_stale_before_any_http(
    database_engine,
    redis_client,
    scene_case,
    gateway_wire,
):
    receipt = ensure(database_engine, scene_case)
    with Session(database_engine) as db, db.begin():
        current = db.get(SceneJob, receipt.job_id)
        current.status = "STALE"
        db.flush()
        historical = SceneJob(
            **(
                current.model_dump()
                | {
                    "id": uuid4(),
                    "frozen_route": None,
                    "status": "PENDING",
                    "dispatch_id": None,
                }
            )
        )
        db.add(historical)
        db.flush()
        historical_id = historical.id
    job = run(database_engine, redis_client, scene_case, historical_id)
    assert job.status == "STALE" and job.error_code == "scene_route_missing"
    assert gateway_wire["wire"].calls == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_same_job_finishes_with_new_token_and_discovery_observation(
    database_engine,
    redis_client,
    scene_case,
    gateway_wire,
):
    from datetime import UTC, datetime

    from app.core.credentials import decrypt_credentials, encrypt_credentials
    from app.modules.accounts.models import BCAccountAccess, DiscoveryRun

    case, route = scene_case, scene_case["route"]
    receipt = ensure(database_engine, case)
    responses = scene_responses(case)
    enqueue(gateway_wire, "identity", responses.pop("identity"))
    first = run(database_engine, redis_client, case, receipt.job_id)
    with Session(database_engine) as db, db.begin():
        conn = db.get(TikTokConnection, route.connection_id)
        values = decrypt_credentials(
            tenant_id=route.tenant_id, ciphertext=conn.credential_ciphertext
        )
        conn.credential_ciphertext = encrypt_credentials(
            tenant_id=route.tenant_id,
            value={**values, "access_token": "synthetic-rotated-token"},
        )
        conn.credential_revision += 1
        discovery = DiscoveryRun(
            tenant_id=route.tenant_id,
            actor_id=case["context"].actor_id,
            connection_id=route.connection_id,
            status="COMPLETE",
            credential_revision=conn.credential_revision,
        )
        db.add(discovery)
        db.flush()
        grant = db.get(
            BCAccountAccess,
            (route.tenant_id, route.bc_id, case["advertiser_id"], route.connection_id),
        )
        previous = grant.last_seen_run_id
        grant.last_seen_run_id, grant.checked_at = discovery.id, datetime.now(UTC)
        discovery_id = discovery.id
    gateway_wire["tokens"].clear()
    try:
        for resource, data in responses.items():
            enqueue(gateway_wire, resource, data)
            final = run(database_engine, redis_client, case, receipt.job_id)
        assert final.id == first.id and final.scope_basis == first.scope_basis
        assert final.status == "COMPLETE", final.error_code
        assert gateway_wire["tokens"]
        assert all(
            value in {"synthetic-rotated-token", "Bearer synthetic-rotated-token"}
            for value in gateway_wire["tokens"]
        )
    finally:
        with Session(database_engine) as db, db.begin():
            grant = db.get(
                BCAccountAccess,
                (
                    route.tenant_id,
                    route.bc_id,
                    case["advertiser_id"],
                    route.connection_id,
                ),
            )
            grant.last_seen_run_id = previous
            db.flush()
            db.delete(db.get(DiscoveryRun, discovery_id))
