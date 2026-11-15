"""真实场景事实、PostgreSQL 与双通道 HTTP；不替换执行器或 gateway。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.modules.builds.execution import process_step
from app.modules.builds.execution_models import (
    ExecutionStep,
    StepEvidence,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.request_compiler import remote_request_id
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.builds.scene.support import enqueue, ensure, run, scene_responses
from tests.modules.builds.scene.support import scene_case as scene_case


@pytest.fixture
def database_engine(isolated_strategy_database):
    return isolated_strategy_database[0]


@pytest.fixture
def retain_build_history(isolated_strategy_database):
    return isolated_strategy_database[0]


@pytest.fixture
def channel_execution(
    database_engine, redis_client, scene_case, gateway_wire, monkeypatch
):
    from app.modules.builds import execution
    from app.modules.builds.drafts import create_draft
    from app.modules.builds.preview_models import (
        BuildPreview,
        BuildUnit,
        PlannedAd,
        PlannedGroup,
        PreviewContract,
        PreviewDrama,
        PreviewDramaGroup,
        PreviewGroupMaterial,
    )
    from app.modules.builds.routes import save_preview_route
    from app.modules.builds.scene import read_scene_context
    from app.modules.materials.models import AccountMaterial
    from app.modules.providers.models import PromotionLink
    from app.modules.strategies.models import CopyEntry, StrategyVersion
    from app.modules.strategies.service import create_strategy
    from tests.modules.materials.test_tenant_materials import material
    from tests.modules.strategies.test_versions import config

    case = scene_case
    context, route = case["context"], case["route"]
    if route.channel == "OFFICIAL_MCP":
        import json
        import time

        from app.integrations.tiktok.mcp import transport

        original_transport = transport._new_http_transport
        diagnostics = []
        gateway_wire["diagnostics"] = diagnostics

        class ObservedTransport(original_transport):
            async def handle_async_request(self, request):
                message = (
                    json.loads(request.content) if request.method == "POST" else {}
                )
                phase = message.get("method", request.method)
                started = time.monotonic()
                try:
                    response = await super().handle_async_request(request)
                except Exception as error:
                    diagnostics.append(
                        (phase, type(error).__name__, time.monotonic() - started)
                    )
                    raise
                diagnostics.append(
                    (phase, response.status_code, time.monotonic() - started)
                )
                return response

        monkeypatch.setattr(transport, "_new_http_transport", ObservedTransport)
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)
    job = ensure(database_engine, case)
    for resource, data in scene_responses(case).items():
        enqueue(gateway_wire, resource, data)
        completed = run(database_engine, redis_client, case, job.job_id)
    assert completed.status == "COMPLETE", completed.error_code
    with Session(database_engine) as db, db.begin():
        scene = read_scene_context(
            db,
            context=context,
            bc_id=route.bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=route,
        )
        assert scene.supported, scene.reason_codes
        strategy = create_strategy(
            db, context=context, name="Synthetic execution", config=config()
        )
        version = db.exec(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy)
        ).one()
        draft = create_draft(
            db,
            context=context,
            bc_id=route.bc_id,
            strategy_version_id=version.id,
            provider_connection_id=case["provider_id"],
            application_id=case["application_id"],
            drama_lines=["Synthetic"],
            account_lines=[case["advertiser_id"]],
            link_config={},
        )
        preview = BuildPreview(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            draft_id=draft,
            draft_revision=1,
            strategy_version_id=version.id,
            actor_id=context.actor_id,
            batch_short_id=uuid4().hex[:12],
            local_date="20260912",
            config=version.config,
            budget=version.budget,
            target_roas=version.target_roas,
            content_digest="a" * 64,
        )
        db.add(preview)
        db.flush()
        # 本夹具刻意保留旧模板，必须显式登记V1布局，不能依靠缺描述符猜历史。
        db.add(
            PreviewContract(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                version=1,
                media_kind="VIDEO",
                legacy_material_layout="GROUP",
            )
        )
        link = db.get(PromotionLink, case["link_id"])
        db.add(
            PreviewDrama(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                drama_id=link.drama_id,
                link_id=link.id,
                title="Synthetic",
                url=link.url,
                protected_base="base",
            )
        )
        db.flush()
        unit = BuildUnit(
            tenant_id=context.tenant_id,
            preview_id=preview.id,
            bc_id=route.bc_id,
            drama_id=link.drama_id,
            advertiser_id=case["advertiser_id"],
            connection_id=route.connection_id,
            currency="USD",
            timezone="UTC",
            campaign_name="Synthetic frozen campaign",
            campaign_digest="b" * 64,
            # 建立旧模板快照，冻结后不再改写，以验证跨版本执行。
            scene_snapshot={
                **scene.to_snapshot(),
                "campaign_fields": {
                    **scene.to_snapshot()["campaign_fields"],
                    "catalog_enabled": False,
                },
            },
            complete=True,
            group_count=1,
            ad_count=1,
        )
        db.add(unit)
        db.flush()
        db.add(
            PreviewDramaGroup(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                drama_id=link.drama_id,
                group_no=1,
            )
        )
        db.flush()
        group = PlannedGroup(
            tenant_id=context.tenant_id,
            preview_id=preview.id,
            bc_id=route.bc_id,
            unit_id=unit.id,
            drama_id=link.drama_id,
            group_no=1,
            name="Synthetic group",
        )
        db.add(group)
        db.flush()
        copy = db.exec(select(CopyEntry).limit(1)).one()
        db.add(
            PlannedAd(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                group_id=group.id,
                creative_no=1,
                name="Synthetic ad",
                copy_id=copy.id,
                text="Watch now",
                cta_option_ids=["cta-watch"],
            )
        )
        file = material(db, context, "Synthetic.mp4", bc=route.bc_id)
        db.add(
            AccountMaterial(
                tenant_id=context.tenant_id,
                bc_id=route.bc_id,
                material_id=file.id,
                advertiser_id=case["advertiser_id"],
                connection_id=route.connection_id,
                video_id="synthetic-video",
                image_id="synthetic-cover",
                status="available",
                verified_at=datetime.now(UTC),
            )
        )
        db.add(
            PreviewGroupMaterial(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                drama_id=link.drama_id,
                group_no=1,
                position=1,
                material_id=file.id,
            )
        )
        save_preview_route(db, context=context, preview_id=preview.id, route=route)
        db.flush()
        preview.status = "FROZEN"
        db.flush()
        sub = Submission(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            preview_id=preview.id,
            draft_id=draft,
            actor_id=context.actor_id,
            ordinal=1,
        )
        db.add(sub)
        db.flush()
        db.add(
            SubmissionUnit(
                tenant_id=context.tenant_id,
                submission_id=sub.id,
                unit_id=unit.id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                disposition="INCLUDED",
                expanded=True,
            )
        )
        db.flush()
        step = ExecutionStep(
            tenant_id=context.tenant_id,
            submission_id=sub.id,
            unit_id=unit.id,
            preview_id=preview.id,
            bc_id=route.bc_id,
            kind="CTA",
            step_key="synthetic-cta",
        )
        db.add(step)
        db.flush()
        db.add(
            ExecutionStep(
                tenant_id=context.tenant_id,
                submission_id=sub.id,
                unit_id=unit.id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                kind="MATERIAL",
                step_key="synthetic-material",
                material_id=file.id,
                status="SUCCEEDED",
                phase="DONE",
            )
        )
        ids = {"CTA": step.id}
        parent = None
        for kind in ("CAMPAIGN", "ADGROUP", "AD"):
            ad = db.exec(select(PlannedAd).where(PlannedAd.group_id == group.id)).one()
            item = ExecutionStep(
                tenant_id=context.tenant_id,
                submission_id=sub.id,
                unit_id=unit.id,
                preview_id=preview.id,
                bc_id=route.bc_id,
                kind=kind,
                step_key="synthetic-" + kind.lower(),
                parent_step_id=parent,
                group_id=group.id if kind != "CAMPAIGN" else None,
                planned_ad_id=ad.id if kind == "AD" else None,
            )
            db.add(item)
            db.flush()
            ids[kind] = item.id
            parent = item.id
        case = {
            **case,
            "step_id": step.id,
            "ids": ids,
            "unit_id": unit.id,
            "submission_id": sub.id,
        }

    gateway_wire["sdk_calls"].clear()
    gateway_wire["wire"].calls.clear()
    yield case, gateway_wire


def invoke(database_engine, redis_client, case, kind="CTA"):
    return process_step(
        database_engine=database_engine,
        redis_client=redis_client,
        context=case["context"],
        step_id=case["ids"][kind],
        revision=0,
    )


def created(wire, kind="CTA"):
    key = {
        "CTA": "creative_portfolio_id",
        "CAMPAIGN": "campaign_id",
        "ADGROUP": "adgroup_id",
        "AD": "smart_plus_ad_id",
    }[kind]
    remote_id = "synthetic-portfolio" if kind == "CTA" else "synthetic-" + kind.lower()
    tool = {
        "CTA": "creative_portfolio_create",
        "CAMPAIGN": "smart_plus_campaign_create",
        "ADGROUP": "smart_plus_adgroup_create",
        "AD": "smart_plus_ad_create",
    }[kind]
    data = {key: remote_id}
    wire["sdk_data"]["data"] = data
    wire["wire"].results[tool].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "data": data,
                "request_id": "safe-request",
                "mcp_request_id": "safe-mcp",
                "remote_task_id": "safe-task",
            },
        }
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_worker_uses_real_gateway_and_keeps_receipt_before_close(
    database_engine, redis_client, channel_execution, monkeypatch
):
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    if case["route"].channel == "OFFICIAL_MCP":
        for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
            monkeypatch.setattr(settings, name, "")
    created(wire)
    assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
    assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
    assert len(business_calls(wire, case["route"].channel)) == 1
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["step_id"])
        assert step.remote_id == "synthetic-portfolio" and step.operation_status is None
        rows = db.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == step.id, StepEvidence.conclusion == "CREATED"
            )
        ).all()
        assert len(rows) == 1 and rows[0].summary["attempt_id"] == str(step.attempt_id)
        if case["route"].channel == "OFFICIAL_MCP":
            assert rows[0].summary["mcp_request_id"] == "safe-mcp"
            assert rows[0].summary["remote_task_id"] == "safe-task"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("change", ["viewer", "claim"])
@pytest.mark.parametrize("boundary", ["initialize", "tools/list"])
def test_initialize_rechecks_current_build_permission_and_claim(
    database_engine, redis_client, channel_execution, monkeypatch, change, boundary
):
    import json

    from app.integrations.tiktok.mcp import transport
    from app.modules.tenants.models import TenantMembership
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    created(wire)
    original = transport._new_http_transport
    replacement = uuid4()

    class AfterInitialize(original):
        async def handle_async_request(self, request):
            message = json.loads(request.content) if request.method == "POST" else {}
            response = await super().handle_async_request(request)
            if message.get("method") == boundary:
                with Session(database_engine) as db, db.begin():
                    if change == "viewer":
                        db.get(
                            TenantMembership,
                            (case["context"].tenant_id, case["context"].actor_id),
                        ).role = "viewer"
                    else:
                        db.get(ExecutionStep, case["step_id"]).lease_token = replacement
            return response

    monkeypatch.setattr(transport, "_new_http_transport", AfterInitialize)
    invoke(database_engine, redis_client, case)
    assert business_calls(wire, "OFFICIAL_MCP") == []
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["step_id"])
        assert step.remote_id is None
        assert (step.request_body is None) == (boundary == "initialize")
        if change == "claim":
            assert step.lease_token == replacement
        else:
            assert (step.status, step.error_code) == ("FAILED", "action_forbidden")


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_mcp_known_ad_rejection_retries_same_frozen_request_once(
    database_engine, redis_client, channel_execution
):
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    assert case["route"].channel == "OFFICIAL_MCP"
    with Session(database_engine) as db, db.begin():
        for kind in ("CTA", "CAMPAIGN", "ADGROUP"):
            step = db.get(ExecutionStep, case["ids"][kind])
            step.status, step.phase = "SUCCEEDED", "DONE"
            step.remote_id = "existing-" + kind.lower()
    wire["wire"].results["smart_plus_ad_create"].append(
        {
            "content": [],
            "structuredContent": {
                "code": 40002,
                "data": {},
                "request_id": "explicit-rejection",
                "message": "synthetic private detail",
            },
        }
    )
    assert invoke(database_engine, redis_client, case, "AD") == "PENDING"
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["ids"]["AD"])
        frozen = (step.request_body, step.request_body_digest, step.attempt_id)
        event = db.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == step.id,
                StepEvidence.conclusion == "REMOTE_REJECTED",
            )
        ).one()
        assert event.summary["remote_code"] == 40002
        assert "synthetic private detail" not in str(event.summary)
    with Session(database_engine) as db, db.begin():
        db.get(ExecutionStep, case["ids"]["AD"]).due_at = datetime.now(UTC)
    created(wire, "AD")
    assert invoke(database_engine, redis_client, case, "AD") == "SUCCEEDED"
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["ids"]["AD"])
        assert (step.request_body, step.request_body_digest, step.attempt_id) == frozen
        assert step.remote_id == "synthetic-ad"
    assert len(business_calls(wire, "OFFICIAL_MCP")) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_accepted_create_drop_is_unknown_and_redelivery_does_not_send(
    database_engine, redis_client, channel_execution, monkeypatch
):
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    if case["route"].channel == "OFFICIAL_MCP":
        wire["wire"].disconnect_after_accept("creative_portfolio_create")
    else:

        def lose(_pool, method, url, **kwargs):
            wire["sdk_calls"].append((method, url, kwargs))
            raise TimeoutError("synthetic remote accepted")

        monkeypatch.setattr("urllib3.PoolManager.request", lose)
    assert invoke(database_engine, redis_client, case) == "UNKNOWN"
    assert invoke(database_engine, redis_client, case) == "UNKNOWN"
    assert len(business_calls(wire, case["route"].channel)) == 1
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["step_id"])
        assert step.request_body_digest and step.request_body and not step.remote_id
        assert step.phase == "REQUEST_ARMED"


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_inflight_disable_preserves_receipt_and_forbids_next_send(
    database_engine, redis_client, channel_execution, monkeypatch
):
    import json

    import urllib3

    from app.integrations.tiktok.mcp import transport
    from app.modules.accounts.models import TikTokConnection
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    created(wire)

    def disable():
        with Session(database_engine) as db, db.begin():
            # HTTP 已进入远端后停用只阻止后继；实际已收到 ID 仍归原 attempt。
            db.get(TikTokConnection, case["route"].connection_id).status = "DISABLED"

    if case["route"].channel == "OFFICIAL_API":
        original = urllib3.PoolManager.request

        def after(pool, method, url, **kwargs):
            response = original(pool, method, url, **kwargs)
            disable()
            return response

        monkeypatch.setattr(urllib3.PoolManager, "request", after)
    else:
        original = transport._new_http_transport

        class AfterResponse(original):
            async def handle_async_request(self, request):
                message = (
                    json.loads(request.content) if request.method == "POST" else {}
                )
                response = await super().handle_async_request(request)
                if message.get("method") == "tools/call":
                    disable()
                return response

        monkeypatch.setattr(transport, "_new_http_transport", AfterResponse)
    assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
    assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
    with Session(database_engine) as db:
        assert db.get(ExecutionStep, case["step_id"]).remote_id == "synthetic-portfolio"
    assert len(business_calls(wire, case["route"].channel)) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_real_gateway_creates_cta_and_all_enabled_layers_with_distinct_attempts(
    database_engine, redis_client, channel_execution
):
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    for kind in ("CTA", "CAMPAIGN", "ADGROUP", "AD"):
        created(wire, kind)
        outcome = invoke(database_engine, redis_client, case, kind)
        with Session(database_engine) as db:
            step = db.get(ExecutionStep, case["ids"][kind])
            assert outcome == "SUCCEEDED", (
                kind,
                step.error_code,
                wire.get("diagnostics", []),
            )
    calls = business_calls(wire, case["route"].channel)
    assert len(calls) == 4
    with Session(database_engine) as db:
        steps = [
            db.get(ExecutionStep, case["ids"][kind])
            for kind in ("CTA", "CAMPAIGN", "ADGROUP", "AD")
        ]
        assert len({step.attempt_id for step in steps}) == 4
        for step in steps[1:]:
            assert step.request_body["operation_status"] == "ENABLE"
        assert steps[2].request_body["campaign_id"] == "synthetic-campaign"
        assert steps[3].request_body["adgroup_id"] == "synthetic-adgroup"
        assert (
            steps[3].request_body["ad_configuration"]["call_to_action_id"]
            == "synthetic-portfolio"
        )
        if case["route"].channel == "OFFICIAL_MCP":
            assert all(
                step.request_body["request_id"] == remote_request_id(step.attempt_id)
                for step in steps[1:3]
            )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("kind", ["CAMPAIGN", "ADGROUP"])
def test_final_quota_denial_requeues_identical_armed_attempt_without_http(
    database_engine, redis_client, channel_execution, monkeypatch, kind
):
    from datetime import timedelta

    from app.integrations.tiktok.admission import quota_scope
    from app.jobs.admission import admission_policy, admit_call, release_call
    from app.modules.builds.dispatch import queue_step
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    created(wire)
    assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
    if kind == "ADGROUP":
        created(wire, "CAMPAIGN")
        assert invoke(database_engine, redis_client, case, "CAMPAIGN") == "SUCCEEDED"
    before_count = len(business_calls(wire, case["route"].channel))
    operation = "build.create_" + kind.lower()
    route = case["route"]
    from app.integrations.tiktok.platform_quotas import sdk_endpoint

    endpoint = sdk_endpoint(operation) if route.channel == "OFFICIAL_API" else operation
    config = {**settings.TIKTOK_CALL_POLICIES}
    config["endpoints"] = {
        **config.get("endpoints", {}),
        operation: {"endpoint_max_inflight": 1},
    }
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", config)
    quota = {
        "app_scope": quota_scope(
            channel=route.channel,
            app_id=settings.TIKTOK_APP_ID,
            verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
        ),
        "endpoint": endpoint,
        "tenant_id": route.tenant_id,
        "advertiser_id": "synthetic-competing-account",
        "lease_id": uuid4(),
    }
    assert admit_call(
        redis_client,
        **quota,
        policy=admission_policy(operation),
        rate_group=endpoint if route.channel == "OFFICIAL_API" else None,
    ).granted
    try:
        assert invoke(database_engine, redis_client, case, kind) == "PENDING"
        assert (
            len(business_calls(wire, route.channel)) == before_count
        )  # 只有已成功父步骤。
        with Session(database_engine) as db:
            step = db.get(ExecutionStep, case["ids"][kind])
            original = (
                step.attempt,
                step.attempt_id,
                step.request_body,
                step.request_body_digest,
            )
            assert (
                step.request_body and step.phase == "IDLE" and step.lease_token is None
            )
            rows = db.exec(
                select(StepEvidence).where(StepEvidence.step_id == step.id)
            ).all()
            assert [row.conclusion for row in rows] == ["REQUEST_ARMED", "NOT_SENT"]
    finally:
        release_call(redis_client, **quota)
    with Session(database_engine) as db, db.begin():
        step = db.get(ExecutionStep, case["ids"][kind])
        step.due_at = datetime.now(UTC) - timedelta(seconds=1)
        queue_step(db, step=step, submission=db.get(Submission, step.submission_id))
        assert step.status == "QUEUED" and step.dispatch_id
        revision = step.dispatch_revision
    created(wire, kind)
    outcome = process_step(
        database_engine=database_engine,
        redis_client=redis_client,
        context=case["context"],
        step_id=case["ids"][kind],
        revision=revision,
    )
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["ids"][kind])
        assert outcome == "SUCCEEDED", step.error_code
        assert (
            step.attempt,
            step.attempt_id,
            step.request_body,
            step.request_body_digest,
        ) == original
        assert step.request_body["request_id"] == remote_request_id(step.attempt_id)
    assert len(business_calls(wire, route.channel)) == before_count + 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_gateway_rejects_quota_lease_shorter_than_worker_deadline(
    database_engine, redis_client, channel_execution, monkeypatch
):
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    config = {**settings.TIKTOK_CALL_POLICIES}
    config["base"] = {**config["base"], "lease_ms": 1000}
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", config)
    assert invoke(database_engine, redis_client, case) == "FAILED"
    assert not business_calls(wire, case["route"].channel)
    with Session(database_engine) as db:
        assert (
            db.get(ExecutionStep, case["step_id"]).error_code
            == "admission_policy_invalid"
        )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_client_cleanup_failure_observes_committed_id_and_never_recreates(
    database_engine, redis_client, channel_execution, monkeypatch
):
    import urllib3

    from app.integrations.tiktok.mcp import transport
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    created(wire)
    observed = []

    def committed():
        with Session(database_engine) as db:
            step = db.get(ExecutionStep, case["step_id"])
            assert (
                step.status == "SUCCEEDED" and step.remote_id == "synthetic-portfolio"
            )
            observed.append(step.remote_id)

    if case["route"].channel == "OFFICIAL_API":
        original = urllib3.PoolManager.clear

        def clear(pool):
            committed()
            original(pool)
            raise RuntimeError("synthetic cleanup failed")

        monkeypatch.setattr(urllib3.PoolManager, "clear", clear)
    else:
        original = transport._new_http_transport

        class FailingClose(original):
            async def aclose(self):
                committed()
                await super().aclose()
                raise RuntimeError("synthetic cleanup failed")

        monkeypatch.setattr(transport, "_new_http_transport", FailingClose)
    assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
    assert invoke(database_engine, redis_client, case) == "SUCCEEDED"
    assert observed and len(business_calls(wire, case["route"].channel)) == 1


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_api_without_app_is_blocked_before_any_create(
    database_engine, redis_client, channel_execution, monkeypatch
):
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, name, "")
    assert invoke(database_engine, redis_client, case) == "FAILED"
    assert not business_calls(wire, "OFFICIAL_API")
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, case["step_id"])
        assert (
            step.error_code == "tiktok_app_not_configured" and step.request_body is None
        )
