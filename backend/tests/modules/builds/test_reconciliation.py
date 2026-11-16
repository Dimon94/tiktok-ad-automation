"""Offline official SDK transport contracts; database cases use real PostgreSQL."""

from types import SimpleNamespace

import pytest

from app.integrations.tiktok.contracts.builds import BuildReadQuery
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.modules.builds import readback_compare
from app.modules.builds.request_compiler import decode_intent
from app.modules.builds.routes import save_attempt_context
from tests.contracts.test_tiktok_build_contract import build_bodies
from tests.integrations.tiktok.build_wire import BuildWire, sdk_build_operations


def test_ad_get_uses_parent_scope_and_never_invents_name_filter(monkeypatch):
    from app.integrations.tiktok.sdk import official_client

    body = build_bodies()["AD"]
    calls = wire(
        monkeypatch,
        [],
        page_transform=lambda _data: {
            "list": [],
            "page_info": {
                "page": 2,
                "page_size": 100,
                "total_number": 100,
                "total_page": 1,
            },
        },
    )
    with official_client(access_token="offline-secret") as client:
        with pytest.raises(RemoteCallError):
            sdk_build_operations(client).read_page(
                query=BuildReadQuery(intent=decode_intent("AD", body), page=2)
            )
    path, query = calls[0]
    assert path == BuildWire.paths["AD"]
    assert query["filtering"] == {"adgroup_ids": [body["adgroup_id"]]}
    assert "ad_name" not in query["filtering"]


def test_field_comparison_requires_target_video_url_copy_and_cta():
    from copy import deepcopy

    body = build_bodies()["AD"]
    query = BuildReadQuery(intent=decode_intent("AD", body), remote_id="remote")

    def compare(actual):
        record = readback_compare.parse_page(
            query=query,
            response=McpBusinessResponse(
                data={
                    "list": [actual],
                    "page_info": {
                        "page": 1,
                        "page_size": 100,
                        "total_number": 1,
                        "total_page": 1,
                    },
                },
                evidence=CallEvidence(),
            ),
        ).rows[0]
        return readback_compare.compare_record(query=query, record=record)

    actual = {**deepcopy(body), "smart_plus_ad_id": "remote"}
    assert compare(actual) == "MATCH"
    actual["creative_list"][0]["creative_info"]["video_info"]["video_id"] = "source-v"
    assert compare(actual) == "MISMATCH"
    actual = {**deepcopy(body), "smart_plus_ad_id": "remote"}
    del actual["ad_configuration"]
    assert compare(actual) == "INCOMPLETE"


@pytest.fixture
def recon_env(isolated_strategy_database, monkeypatch, redis_client):
    from uuid import uuid4

    from cryptography.fernet import Fernet
    from sqlmodel import Session, select

    from app.core.config import settings
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.models import TikTokConnection
    from app.modules.builds import previews, reconciliation, submissions
    from app.modules.builds.execution_models import ExecutionStep
    from tests.modules.builds.test_drafts import create_intent
    from tests.modules.builds.test_previews import drain, prepared

    engine, context, _ = isolated_strategy_database
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"reconciliation-{uuid4()}")
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "offline-secret")
    monkeypatch.setattr(
        settings, "TIKTOK_REDIRECT_URI", "https://example.test/callback"
    )
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            "base": {
                "app_max_inflight": 10,
                "endpoint_max_inflight": 1,
                "tenant_max_inflight": 10,
                "advertiser_max_inflight": 1,
                "app_calls_per_window": 10000,
                "endpoint_calls_per_window": 10000,
                "window_ms": 1000,
                "lease_ms": 60000,
            }
        },
    )
    monkeypatch.setattr(
        reconciliation,
        "current_task",
        SimpleNamespace(
            request=SimpleNamespace(
                timelimit=(45, 40), called_directly=False, is_eager=False
            )
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
    with Session(engine) as session:
        draft = prepared.__wrapped__(
            session, context, create_intent(session, context), monkeypatch
        )
        preview = previews.generate_preview(
            session, context=context, draft_id=draft, expected_revision=1
        )
        drain(session, context, preview)
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview, request_id=uuid4()
        )
        while not submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id
        ):
            pass
        from tests.modules.builds.legacy_readbacks import add_legacy_readbacks

        add_legacy_readbacks(session, receipt.submission_id)
        for connection in session.exec(
            select(TikTokConnection).where(
                TikTokConnection.tenant_id == context.tenant_id
            )
        ).all():
            connection.credential_ciphertext = encrypt_credentials(
                tenant_id=context.tenant_id,
                value={"access_token": "offline-private-token"},
            )
            session.add(connection)
        steps = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        session.commit()
        ids = {
            kind: next(step.id for step in steps if step.kind == kind)
            for kind in ("CAMPAIGN", "ADGROUP", "AD", "CTA")
        }
    yield SimpleNamespace(engine=engine, context=context, ids=ids, redis=redis_client)
    # Unique application scope; rate keys expire, remove only these test keys.
    from app.jobs.admission import admission_keys

    for endpoint in BuildWire.paths.values():
        for advertiser in ("A", "B", "C"):
            redis_client.delete(
                *admission_keys(
                    settings.TIKTOK_APP_ID, endpoint, context.tenant_id, advertiser
                )
            )


def arm(env, kind="CAMPAIGN", *, known=None, readback=False, mcp_request_id=None):
    import json
    from hashlib import sha256

    from sqlmodel import Session, select

    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.preview_models import BuildUnit

    with Session(env.engine) as session:
        step = session.get(ExecutionStep, env.ids[kind])
        unit = session.get(BuildUnit, step.unit_id)
        from tests.contracts.test_tiktok_build_contract import build_bodies

        body = {**build_bodies()[kind], "advertiser_id": unit.advertiser_id}
        if kind == "CAMPAIGN":
            body.update(
                campaign_name="frozen exact name",
                budget="100.00",
                budget_optimize_on=True,
                operation_status="ENABLE",
            )
        elif kind == "ADGROUP":
            body.update(
                campaign_id="campaign-parent",
                adgroup_name="frozen exact group",
                roas_bid="1.2",
                operation_status="ENABLE",
            )
        elif kind == "AD":
            body.update(
                adgroup_id="group-parent",
                ad_name="frozen exact ad",
                operation_status="ENABLE",
                creative_list=[
                    {
                        "creative_info": {
                            "video_info": {"video_id": "target-vid"},
                            "image_info": [{"web_uri": "target-cover"}],
                            "identity_id": "identity",
                            "identity_type": "BC_AUTH_TT",
                            "identity_authorized_bc_id": unit.bc_id,
                            "ad_format": "SINGLE_VIDEO",
                        }
                    }
                ],
                ad_text_list=[{"ad_text": "frozen copy"}],
                landing_page_url_list=[
                    {"landing_page_url": "https://example.test/frozen"}
                ],
                ad_configuration={"call_to_action_id": "cta-actual"},
            )
        else:
            body.update(
                creative_portfolio_type="CTA",
                portfolio_content=[
                    {"asset_content": "Learn more", "asset_ids": ["asset-actual"]}
                ],
            )
        from app.modules.builds.routes import load_preview_route

        route = load_preview_route(
            session, context=env.context, preview_id=step.preview_id
        )
        if route.channel == "OFFICIAL_MCP" and kind in {"CAMPAIGN", "ADGROUP"}:
            original_attempt = str(save_attempt_context(session, step=step))
            body["request_id"] = (
                original_attempt if mcp_request_id is None else mcp_request_id
            )
            session.flush()
        step.request_body = body
        step.request_body_digest = sha256(
            json.dumps(
                body, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        step.status, step.phase = (
            ("SUCCEEDED", "DONE") if known else ("UNKNOWN", "REQUEST_ARMED")
        )
        step.remote_id = known
        session.add(step)
        from app.modules.builds.execution_state import evidence

        evidence(
            session,
            step=step,
            claim=None,
            conclusion="REQUEST_ARMED",
            summary={"body_digest": step.request_body_digest},
        )
        identity = step.id
        if readback:
            identity = session.exec(
                select(ExecutionStep.id).where(
                    ExecutionStep.parent_step_id == step.id,
                    ExecutionStep.kind == "READBACK",
                )
            ).one()
        session.commit()
        return identity, body


def wire(monkeypatch, rows, *, hook=None, page_transform=None, fail=False):
    """Actual generated SDK calls reach the transport; no workflow is mocked."""
    from copy import deepcopy

    calls = []

    import json
    from urllib.parse import urlsplit

    from urllib3.response import HTTPResponse

    def transport(_pool, method, url, **kwargs):
        path = urlsplit(url).path
        assert method == "GET" and path in BuildWire.paths.values()
        query = dict(kwargs.get("fields", ()))
        for key in ("filtering", "fields"):
            if isinstance(query.get(key), str):
                query[key] = json.loads(query[key])
        calls.append((path, query))
        if hook:
            hook(path, query)
        if fail:
            raise OSError("offline-private-token must never be persisted")
        selected = rows(path, query) if callable(rows) else rows
        if "portfolio" in path:
            data = deepcopy(selected)
        else:
            page = query["page"]
            data = {
                "list": deepcopy(selected[(page - 1) * 100 : page * 100]),
                "page_info": {
                    "page": page,
                    "page_size": 100,
                    "total_number": len(selected),
                    "total_page": (len(selected) + 99) // 100,
                },
            }
            if page_transform:
                page_transform(data)
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "offline-read", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    return calls


def next_revision(env, identity):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        step.dispatch_revision += 1
        revision = step.dispatch_revision
        session.add(step)
        session.commit()
        return revision


def run(env, identity, revision=0):
    from app.modules.builds.reconciliation import process_reconciliation

    return process_reconciliation(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        step_id=identity,
        revision=revision,
    )


def test_unknown_campaign_remote_effect_is_recovered_without_create_or_database_lock(
    recon_env, monkeypatch, caplog
):
    from sqlmodel import Session, select

    from app.core.config import settings
    from app.integrations.tiktok.platform_quotas import sdk_endpoint
    from app.jobs.admission import admission_keys
    from app.modules.builds.execution_models import ExecutionStep, StepEvidence

    env = recon_env
    identity, body = arm(env)
    # Gateway已按真实物理接口准入，不能再查询废弃的逻辑端点桶。
    endpoint = sdk_endpoint("build.get_campaigns")

    def hook(_path, _query):
        with Session(env.engine) as session:
            step = session.exec(
                select(ExecutionStep)
                .where(ExecutionStep.id == identity)
                .with_for_update(nowait=True)
            ).one()
            assert step.lease_token and step.status == "UNKNOWN"
            keys = admission_keys(
                settings.TIKTOK_APP_ID,
                endpoint,
                env.context.tenant_id,
                body["advertiser_id"],
            )
            assert all(env.redis.zcard(key) == 1 for key in keys[2:])

    calls = wire(
        monkeypatch,
        [
            {
                **body,
                "campaign_id": "created-but-response-lost",
                "budget": 100,
                "secondary_status": "CAMPAIGN_STATUS_ENABLE",
            }
        ],
        hook=hook,
    )
    assert run(env, identity).state == "SUCCEEDED"
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        assert (
            step.remote_id == "created-but-response-lost"
            and step.checked_at
            and step.request_body == body
        )
        assert not step.mismatch and step.review_status == "CAMPAIGN_STATUS_ENABLE"
        from app.modules.ads.models import AdObject

        obj = session.get(
            AdObject,
            (step.tenant_id, body["advertiser_id"], "campaign", step.remote_id),
        )
        assert obj is not None and obj.name == body["campaign_name"]
        evidence = session.exec(
            select(StepEvidence).where(StepEvidence.step_id == identity)
        ).all()
        assert evidence and "offline-private-token" not in str(
            [row.summary for row in evidence]
        )
    assert len(calls) == 1 and "offline-private-token" not in caplog.text
    assert all(
        env.redis.zcard(key) == 0
        for key in admission_keys(
            settings.TIKTOK_APP_ID,
            endpoint,
            env.context.tenant_id,
            body["advertiser_id"],
        )[2:]
    )


def test_ad_second_page_and_new_dispatch_resume_without_name_filter(
    recon_env, monkeypatch
):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env, "AD")
    rows = [
        {**body, "ad_name": f"other-{i}", "smart_plus_ad_id": f"id-{i}"}
        for i in range(100)
    ]
    rows.append({**body, "smart_plus_ad_id": "actual-ad"})
    calls = wire(monkeypatch, rows)
    first = run(env, identity)
    assert first.needs_more and len(calls) == 1
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        assert step.remote_id is None and step.resolved["reconciliation"]["page"] == 2
        step.dispatch_revision += 1
        session.add(step)
        session.commit()
    assert run(env, identity, revision=1).state == "SUCCEEDED"
    assert len(calls) == 2 and all(
        call[1]["filtering"] == {"adgroup_ids": ["group-parent"]} for call in calls
    )


@pytest.mark.parametrize(
    "case", ["empty", "ambiguous", "missing", "wrong_parent", "different_video"]
)
def test_inconclusive_read_never_proves_absence_or_recreates(
    recon_env, monkeypatch, case
):
    from copy import deepcopy

    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env, "AD")
    rows = [{**deepcopy(body), "smart_plus_ad_id": "candidate"}]
    if case == "empty":
        rows = []
    if case == "ambiguous":
        rows.append({**body, "smart_plus_ad_id": "second"})
    if case == "missing":
        del rows[0]["ad_text_list"]
    if case == "wrong_parent":
        rows[0]["adgroup_id"] = "foreign-parent"
    if case == "different_video":
        rows[0]["creative_list"][0]["creative_info"]["video_info"]["video_id"] = (
            "source-vid"
        )
    calls = wire(monkeypatch, rows)
    result = run(env, identity)
    assert result.state == "UNKNOWN" and not result.needs_more and len(calls) == 1
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        assert (
            step.status == "UNKNOWN"
            and step.remote_id is None
            and step.request_body == body
        )


def test_adgroup_status_is_a_separate_get_and_readback_never_disables(
    recon_env, monkeypatch
):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env, "ADGROUP", known="actual-group", readback=True)
    smart = {
        **body,
        "adgroup_id": "actual-group",
        "secondary_status": "ADGROUP_STATUS_AUDIT",
    }
    del smart["operation_status"]

    def rows(path, _query):
        return (
            [{**smart, "operation_status": "DISABLE"}]
            if path == BuildWire.paths["ADGROUP_STATUS"]
            else [smart]
        )

    calls = wire(monkeypatch, rows)
    assert run(env, identity).needs_more and len(calls) == 1
    assert (
        run(env, identity, next_revision(env, identity)).state == "MISMATCH"
        and len(calls) == 2
    )
    with Session(env.engine) as session:
        source = session.get(ExecutionStep, env.ids["ADGROUP"])
        assert source.status == "SUCCEEDED" and source.remote_id == "actual-group"
        assert (
            source.review_status == "ADGROUP_STATUS_AUDIT"
            and source.operation_status == "DISABLE"
            and source.mismatch
            and source.checked_at
        )
    assert calls[1][0] == BuildWire.paths["ADGROUP_STATUS"]
    # 回读可有第二次分页尝试，创建步骤仍是自己的原始计数与 companion。
    from sqlmodel import select

    from app.modules.builds.execution_models import StepEvidence
    from app.modules.builds.route_models import BuildAttemptContext

    with Session(env.engine) as session:
        readback = session.get(ExecutionStep, identity)
        source = session.get(ExecutionStep, readback.parent_step_id)
        event = session.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == source.id,
                StepEvidence.conclusion == "RECONCILED",
            )
        ).one()
        own = session.get(
            BuildAttemptContext, (source.tenant_id, source.id, event.attempt)
        )
        assert event.attempt == source.attempt != readback.attempt
        assert own.attempt_id == source.attempt_id != readback.attempt_id


def test_cta_requires_known_receipt_and_exact_text_id_binding(recon_env, monkeypatch):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep, StepEvidence

    env = recon_env
    identity, body = arm(env, "CTA")
    calls = wire(monkeypatch, {**body, "creative_portfolio_id": "actual-cta"})
    assert run(env, identity).state == "UNKNOWN" and not calls
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        save_attempt_context(session, step=step)
        session.add(
            StepEvidence(
                tenant_id=step.tenant_id,
                submission_id=step.submission_id,
                step_id=step.id,
                attempt=step.attempt,
                conclusion="LATE_CREATED",
                summary={"remote_id": "actual-cta"},
            )
        )
        session.commit()
    assert (
        run(env, identity, next_revision(env, identity)).state == "SUCCEEDED"
        and len(calls) == 1
    )
    assert calls[0][1]["creative_portfolio_id"] == "actual-cta"


@pytest.mark.parametrize("mode", ["duplicate_id", "changed_total"])
def test_full_page_consistency_is_required(recon_env, monkeypatch, mode):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env, "AD")

    def rows(_path, query):
        values = [
            {**body, "ad_name": f"other-{i}", "smart_plus_ad_id": f"id-{i}"}
            for i in range(100)
        ]
        values.append(
            {**body, "smart_plus_ad_id": "id-0" if mode == "duplicate_id" else "actual"}
        )
        if mode == "changed_total" and query["page"] == 2:
            values.append(
                {**body, "ad_name": "later insert", "smart_plus_ad_id": "new"}
            )
        return values

    calls = wire(monkeypatch, rows)
    assert run(env, identity).needs_more
    assert (
        run(env, identity, next_revision(env, identity)).state == "UNKNOWN"
        and len(calls) == 2
    )
    with Session(env.engine) as session:
        assert session.get(ExecutionStep, identity).remote_id is None


def test_duplicate_workers_and_late_response_cannot_overwrite_new_nonce(
    recon_env, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta
    from threading import Event

    from sqlmodel import Session, select

    from app.modules.builds import reconciliation
    from app.modules.builds.execution_models import ExecutionStep, StepEvidence

    env = recon_env
    identity, body = arm(env)
    entered, release = Event(), Event()

    def hook(_path, _query):
        entered.set()
        assert release.wait(10)

    calls = wire(monkeypatch, [{**body, "campaign_id": "actual-campaign"}], hook=hook)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, env, identity)
        assert entered.wait(10)
        try:
            duplicate = run(env, identity)
            assert duplicate.state == "BUSY" and len(calls) == 1
            with Session(env.engine) as session:
                step = session.get(ExecutionStep, identity)
                step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                step.dispatch_revision = 1
                session.add(step)
                session.commit()
            with Session(env.engine) as session, session.begin():
                new_claim = reconciliation._claim(session, env.context, identity, 1)
                assert not isinstance(new_claim, reconciliation.ReconciliationResult)
            release.set()
            assert future.result(10).state == "STALE"
        finally:
            release.set()
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        assert (
            step.remote_id is None
            and step.lease_token == new_claim.token
            and step.attempt == new_claim.attempt
        )
        assert session.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == identity,
                StepEvidence.conclusion == "LATE_READBACK",
            )
        ).one()


def test_latest_actor_permission_is_checked_after_admission_and_can_recover(
    recon_env, monkeypatch
):
    from contextlib import contextmanager

    from sqlmodel import Session

    from app.modules.builds import reconciliation
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.tenants.models import TenantMembership

    env = recon_env
    identity, body = arm(env)
    real = reconciliation.admitted_build_call

    @contextmanager
    def revoke(*args, **kwargs):
        with real(*args, **kwargs):
            with Session(env.engine) as session:
                membership = session.get(
                    TenantMembership, (env.context.tenant_id, env.context.actor_id)
                )
                membership.role = "viewer"
                session.add(membership)
                session.commit()
            yield

    monkeypatch.setattr(reconciliation, "admitted_build_call", revoke)
    calls = wire(monkeypatch, [{**body, "campaign_id": "actual"}])
    assert run(env, identity).state == "UNKNOWN" and not calls
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        assert step.error_code == "action_forbidden" and step.lease_token is None
        membership = session.get(
            TenantMembership, (env.context.tenant_id, env.context.actor_id)
        )
        membership.role = "operator"
        session.add(membership)
        session.commit()
    monkeypatch.setattr(reconciliation, "admitted_build_call", real)
    assert (
        run(env, identity, next_revision(env, identity)).state == "SUCCEEDED"
        and len(calls) == 1
    )


def test_changed_connection_and_foreign_context_never_read_remote(
    recon_env, monkeypatch
):
    from uuid import uuid4

    from sqlmodel import Session, select

    from app.core.context import TenantContext
    from app.core.errors import DomainError
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection
    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env)
    calls = wire(monkeypatch, [])
    with pytest.raises(DomainError, match="步骤不存在"):
        from app.modules.builds.reconciliation import process_reconciliation

        process_reconciliation(
            database_engine=env.engine,
            redis_client=env.redis,
            context=TenantContext(
                tenant_id=uuid4(), actor_id=env.context.actor_id, role="owner"
            ),
            step_id=identity,
            revision=0,
        )
    with Session(env.engine) as session:
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == env.context.tenant_id,
                BCAccountAccess.advertiser_id == body["advertiser_id"],
            )
        ).one()
        replacement = TikTokConnection(tenant_id=env.context.tenant_id, status="ACTIVE")
        session.add(replacement)
        session.flush()
        grant.active = False
        session.add(grant)
        session.add(
            BCAccountAccess(
                tenant_id=grant.tenant_id,
                bc_id=grant.bc_id,
                advertiser_id=grant.advertiser_id,
                connection_id=replacement.id,
                active=True,
                authorized=True,
                in_bc=True,
                permission_state="VERIFIED",
                can_build=True,
            )
        )
        session.commit()
    assert run(env, identity).state == "UNKNOWN" and not calls
    with Session(env.engine) as session:
        assert (
            session.get(ExecutionStep, identity).error_code == "account_access_denied"
        )


def test_redis_denial_never_gets_and_does_not_claim_sending(recon_env, monkeypatch):
    from uuid import uuid4

    from sqlmodel import Session

    from app.core.config import settings
    from app.jobs.admission import admission_policy, admit_call, release_call
    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env)
    endpoint = BuildWire.operations["CAMPAIGN"]
    owner = uuid4()
    scope = {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": endpoint,
        "tenant_id": env.context.tenant_id,
        "advertiser_id": body["advertiser_id"],
        "lease_id": owner,
    }
    assert admit_call(env.redis, **scope, policy=admission_policy(endpoint)).granted
    calls = wire(monkeypatch, [])
    try:
        result = run(env, identity)
        assert result.needs_more and result.retry_after_seconds > 0 and not calls
        with Session(env.engine) as session:
            step = session.get(ExecutionStep, identity)
            assert (
                step.status == "UNKNOWN"
                and step.lease_token is None
                and step.remote_id is None
            )
    finally:
        release_call(env.redis, **scope)


@pytest.mark.parametrize("mode", ["eager", "solo", "direct", "long", "missing"])
def test_production_guard_rejects_unbounded_execution(monkeypatch, mode):
    from app.core.errors import DomainError
    from app.modules.builds import reconciliation

    request = SimpleNamespace(timelimit=(45, 40), called_directly=False, is_eager=False)
    process = SimpleNamespace(daemon=True, name="ForkPoolWorker-1")
    if mode == "eager":
        request.is_eager = True
    if mode == "solo":
        process.name = "MainProcess"
    if mode == "direct":
        request.called_directly = True
    if mode == "long":
        request.timelimit = (900, 40)
    if mode == "missing":
        request.timelimit = None
    monkeypatch.setattr(
        reconciliation, "current_task", SimpleNamespace(request=request)
    )
    monkeypatch.setattr(reconciliation, "current_process", lambda: process)
    with pytest.raises(DomainError, match="有界"):
        reconciliation.require_bounded_worker()


def test_actual_create_effect_loses_response_then_readback_recovers_once(
    recon_env, monkeypatch
):
    from uuid import uuid4

    from sqlmodel import Session, select

    from app.integrations.tiktok.sdk import official_client
    from app.modules.builds import execution_state, submissions
    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity = env.ids["CAMPAIGN"]
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        for dependency in session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == step.unit_id,
                ExecutionStep.kind.in_(["MATERIAL", "CTA"]),
            )
        ).all():
            dependency.status, dependency.phase = "SUCCEEDED", "DONE"
            session.add(dependency)
        session.flush()
        claim = submissions.claim_step(
            session, context=env.context, step_id=identity, owner=uuid4()
        )
        assert claim
        body = {
            **build_bodies()["CAMPAIGN"],
            "advertiser_id": claim.advertiser_id,
            "campaign_name": "frozen-lost-response",
            "budget": 100,
            "budget_optimize_on": True,
            "operation_status": "ENABLE",
        }
        execution_state.arm_request(
            session, context=env.context, claim=claim, body=body
        )
        session.commit()
    store, calls = [], []

    import json
    from urllib.parse import urlsplit

    from urllib3.response import HTTPResponse

    def transport(_pool, method, url, **kwargs):
        path = urlsplit(url).path
        calls.append((path, method))
        if method == "POST":
            assert path == BuildWire.create_paths["CAMPAIGN"] and not store
            store.append(
                {**json.loads(kwargs["body"]), "campaign_id": "effect-committed"}
            )
            raise TimeoutError("response lost after remote commit")
        assert method == "GET" and path == BuildWire.paths["CAMPAIGN"]
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "list": store,
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_number": 1,
                            "total_page": 1,
                        },
                    },
                    "request_id": "read-after-loss",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    with official_client(access_token="offline-private-token") as client:
        with pytest.raises(RemoteCallError):
            sdk_build_operations(client).create(
                attempt_id=claim.attempt_id, intent=decode_intent("CAMPAIGN", body)
            )
    with Session(env.engine) as session:
        execution_state.record_unknown(
            session, claim=claim, code="create_result_unknown"
        )
        step = session.get(ExecutionStep, identity)
        # Production hard timeout/lease expiry precedes takeover. The fake clock
        # advances the database expiry, without waiting or inventing an absence.
        from datetime import UTC, datetime, timedelta

        step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(step)
        session.commit()
    assert run(env, identity).state == "SUCCEEDED"
    assert run(env, identity).state == "SUCCEEDED"  # Lost ACK returns saved delivery.
    assert [method for _, method in calls] == ["POST", "GET"]
    with Session(env.engine) as session:
        assert session.get(ExecutionStep, identity).remote_id == "effect-committed"


@pytest.mark.parametrize(
    "kind,status_only",
    [
        ("CAMPAIGN", False),
        ("ADGROUP", False),
        ("AD", False),
        ("ADGROUP", True),
        ("CTA", False),
    ],
)
def test_pinned_sdk_real_serialization_and_async_cleanup(
    monkeypatch, kind, status_only, caplog
):
    import json

    from urllib3.response import HTTPResponse

    from app.integrations.tiktok.sdk import official_client

    body = {**build_bodies()[kind], "advertiser_id": "actual-account"}
    row = {**body, BuildWire.id_keys[kind]: "actual-id"}
    data = (
        row
        if kind == "CTA"
        else {
            "list": [row],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_number": 1,
                "total_page": 1,
            },
        }
    )
    calls = []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "official-envelope", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="never-log-this-token") as client:
        adapter = sdk_build_operations(client)
        result = (
            adapter.read_adgroup_status(
                advertiser_id=body["advertiser_id"], adgroup_id="actual-id"
            )
            if status_only
            else adapter.read_page(
                query=BuildReadQuery(
                    intent=decode_intent(kind, body), remote_id="actual-id"
                )
            )
        )
        assert result.evidence.request_id == "official-envelope"
        if not status_only:
            assert result.rows[0].remote_id == "actual-id"
    assert "Access-Token" not in client.default_headers
    assert len(calls) == 1 and calls[0][0] == "GET"
    endpoint = BuildWire.paths["ADGROUP_STATUS" if status_only else kind]
    assert calls[0][1].endswith(endpoint)
    fields = dict(calls[0][2]["fields"])
    assert fields["advertiser_id"] == "actual-account" and "access_token" not in fields
    if kind != "CTA":
        filtering = json.loads(fields["filtering"])
        assert filtering[BuildWire.id_keys[kind] + "s"] == ["actual-id"]
        assert "ad_name" not in filtering
    assert "never-log-this-token" not in caplog.text


def test_conflicting_late_receipts_never_pick_an_arbitrary_id(recon_env, monkeypatch):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep, StepEvidence

    env = recon_env
    identity, _ = arm(env)
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        save_attempt_context(session, step=step, attempt=1)
        for remote in ("one", "two"):
            session.add(
                StepEvidence(
                    tenant_id=step.tenant_id,
                    submission_id=step.submission_id,
                    step_id=step.id,
                    attempt=1,
                    conclusion="LATE_CREATED",
                    summary={"remote_id": remote},
                )
            )
        session.commit()
    calls = wire(monkeypatch, [])
    assert run(env, identity).state == "UNKNOWN" and not calls


def test_known_id_budget_difference_keeps_created_count_and_marks_mismatch(
    recon_env, monkeypatch
):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env, known="actual-campaign", readback=True)
    calls = wire(
        monkeypatch, [{**body, "campaign_id": "actual-campaign", "budget": 999}]
    )
    assert run(env, identity).state == "MISMATCH"
    assert run(env, identity).state == "MISMATCH" and len(calls) == 1
    with Session(env.engine) as session:
        source = session.get(ExecutionStep, env.ids["CAMPAIGN"])
        assert (
            source.remote_id == "actual-campaign"
            and source.status == "SUCCEEDED"
            and source.mismatch
        )


def test_unknown_adgroup_cannot_bind_if_observed_roas_differs(recon_env, monkeypatch):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, body = arm(env, "ADGROUP")
    smart = {**body, "adgroup_id": "candidate-group", "roas_bid": 99}
    del smart["operation_status"]

    def rows(path, _query):
        return (
            [{**smart, "roas_bid": 99, "operation_status": "ENABLE"}]
            if path == BuildWire.paths["ADGROUP_STATUS"]
            else [smart]
        )

    calls = wire(monkeypatch, rows)
    assert run(env, identity).state == "UNKNOWN"
    assert len(calls) == 1
    with Session(env.engine) as session:
        assert session.get(ExecutionStep, identity).remote_id is None


def test_early_delivery_preserves_due_time_without_get(recon_env, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep

    env = recon_env
    identity, _ = arm(env)
    due = datetime.now(UTC) + timedelta(seconds=120)
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        step.due_at = due
        session.add(step)
        session.commit()
    calls = wire(monkeypatch, [])
    result = run(env, identity)
    assert result.needs_more and 118 <= result.retry_after_seconds <= 120 and not calls
    with Session(env.engine) as session:
        step = session.get(ExecutionStep, identity)
        assert step.due_at == due and step.attempt == 0 and step.lease_token is None
