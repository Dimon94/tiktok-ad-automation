"""Committed isolated PostgreSQL + actual official SDK serialization, offline wire."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.modules.accounts.models import TikTokConnection
from app.modules.builds import previews, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from app.modules.builds.scene_schemas import SceneContext
from app.modules.materials.models import AccountMaterial
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.service import append_version
from tests.modules.builds.test_drafts import account, create_intent, finish, ready_links
from tests.modules.builds.test_execution_admission import policy
from tests.modules.builds.test_previews import drain, seed_targeting_directory
from tests.modules.materials.test_tenant_materials import material
from tests.modules.strategies.test_versions import config


@pytest.fixture
def executable(isolated_strategy_database, monkeypatch, request):
    from app.modules.builds import execution, execution_window
    from app.modules.builds.drafts import create_draft, prepare_draft

    db, context, _ = isolated_strategy_database
    # 故障/接续基础用例使用一个槽以强制跨窗口；并行用例显式验证默认十槽。
    monkeypatch.setattr(execution_window, "MAX_ACTIVE_UNITS", 1)
    policy(monkeypatch)
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)
    scene = SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="offline-v1",
        name_limit=512,
        creative_limit=50,
        copy_length_limit=100,
        campaign_fields={
            "objective_type": "APP_PROMOTION",
            "app_promotion_type": "MINIS",
            "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
        },
        adgroup_fields={
            "minis_id": "minis-1",
            "promotion_type": "MINI_APP",
            "optimization_goal": "VALUE",
            "optimization_event": "ACTIVE_PAY",
            "bid_type": "BID_TYPE_NO_BID",
            "deep_bid_type": "VO_MIN_ROAS",
            "billing_event": "OCPM",
            "targeting_spec": {"location_ids": ["fixture-location"]},
            "placement_type": "PLACEMENT_TYPE_NORMAL",
            "placements": ["PLACEMENT_TIKTOK"],
        },
        creative_fields={
            "creative_info": {
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity-1",
                "identity_authorized_bc_id": "bc-draft",
                "ad_format": "SINGLE_VIDEO",
            }
        },
        cta_fields={
            "asset_ids": ["cta-1"],
            "recommend_assets": [
                {"asset_ids": ["cta-1"], "asset_content": "Watch now"}
            ],
            "requires_portfolio_creation": True,
        },
        field_constraints={
            "target_regions": [
                {"region_code": "US", "location_id": "fixture-location"}
            ],
            "name_limits": {"campaign": 512, "adgroup": 512, "ad": 512},
            "name_measurement": {
                "campaign": "characters",
                "adgroup": "characters",
                "ad": "characters",
            },
            "copy_measurement": "characters",
            "max_ads_per_adgroup": 30,
            "roas_bid": {"minimum": "0.01", "maximum": "1000"},
            "campaign_daily_budget": {
                "currency": "USD",
                "minimum_inclusive": "50",
                "maximum_exclusive": "10000000",
                "precision": "0.01",
            },
        },
    )
    monkeypatch.setattr(previews, "read_scene_context", lambda *a, **k: scene)
    monkeypatch.setattr(execution, "read_scene_context", lambda *a, **k: scene)
    with Session(db) as session:
        intent = create_intent(session, context)
        version = session.get(StrategyVersion, intent["strategy_version_id"])
        accounts = [
            f"account-{chr(65 + i)}" for i in range(getattr(request, "param", 1))
        ]
        intent.update(
            strategy_version_id=append_version(
                session,
                context=context,
                strategy_id=version.strategy_id,
                config=config(
                    max_materials_per_group=2, ads_per_group=1, creative_count=2
                ),
            ),
            drama_lines=["Moon"],
            account_lines=accounts,
        )
        for identity in accounts:
            account(session, context, identity)
        connection = session.exec(
            select(TikTokConnection).where(
                TikTokConnection.tenant_id == context.tenant_id
            )
        ).one()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id,
            value={"access_token": "fixture-secret", "scope": "[2,6]"},
        )
        for i in range(2):
            file = material(session, context, f"Moon-{i}.mp4", bc="bc-draft")
            for identity in accounts:
                session.add(
                    AccountMaterial(
                        tenant_id=context.tenant_id,
                        bc_id="bc-draft",
                        material_id=file.id,
                        advertiser_id=identity,
                        connection_id=connection.id,
                        video_id=f"target-{i}",
                        image_id=f"cover-{i}",
                        status="available",
                        verified_at=datetime.now(UTC),
                    )
                )
        session.flush()
        draft = create_draft(session, context=context, **intent)
        task = prepare_draft(
            session, context=context, draft_id=draft, request_id=uuid4()
        )
        ready_links(session, context, task, intent)
        finish(session, context, task)
        from app.modules.builds.mini_targets import remember_target

        remember_target(
            session,
            context=context,
            url="https://example.com/drama",
            minis_id="minis-1",
            source="USER",
        )
        seed_targeting_directory(session, context, accounts, minis_id="minis-1")
        preview = previews.generate_preview(
            session, context=context, draft_id=draft, expected_revision=1
        )
        drain(session, context, preview)
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview, request_id=uuid4()
        )
        while not submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id, limit=100
        ):
            pass
        steps = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        identities = {
            kind: [s.id for s in steps if s.kind == kind]
            for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD", "READBACK"]
        }
        session.commit()
    return db, context, identities


def run(env, redis_client, kind):
    from app.modules.builds.execution import process_step

    db, context, ids = env
    for step in ids[kind]:
        process_step(
            database_engine=db,
            redis_client=redis_client,
            context=context,
            step_id=step,
            revision=0,
        )


def test_all_layers_enable_target_assets_and_no_row_locks_during_official_wire(
    executable, redis_client, monkeypatch
):
    db, context, ids = executable
    calls = []

    def request(_pool, method, url, **kwargs):
        assert method == "POST"
        body = json.loads(kwargs["body"])
        calls.append((url, body))
        with Session(db) as check:
            armed = check.exec(
                select(ExecutionStep)
                .where(
                    ExecutionStep.status == "RUNNING",
                    ExecutionStep.phase == "REQUEST_ARMED",
                )
                .with_for_update(nowait=True)
            ).one()
            assert armed.request_body == body and armed.request_body_digest
        kind = "cta" if "portfolio" in url else url.split("/")[-3]
        key = {
            "cta": "creative_portfolio_id",
            "campaign": "campaign_id",
            "adgroup": "adgroup_id",
            "ad": "smart_plus_ad_id",
        }[kind]
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {key: f"{kind}-{len(calls)}", "operation_status": "ENABLE"},
                    "request_id": f"req-{len(calls)}",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"]:
        run(executable, redis_client, kind)
    assert len(calls) == 5
    assert all(
        b["operation_status"] == "ENABLE" for u, b in calls if "portfolio" not in u
    )
    group = next(b for u, b in calls if "/adgroup/" in u)
    assert "budget" not in group and group["roas_bid"] == 1.08
    assert "targeting_optimization_mode" not in group
    assert group["targeting_spec"] == {
        "location_ids": ["fixture-location"],
    }
    ads = [b for u, b in calls if "/ad/create/" in u]
    assert ads[0]["creative_list"] == ads[1]["creative_list"]
    assert (
        len(ads[0]["creative_list"]) == 2
        and ads[0]["ad_text_list"] != ads[1]["ad_text_list"]
    )
    assert {
        c["creative_info"]["video_info"]["video_id"] for c in ads[0]["creative_list"]
    } == {"target-0", "target-1"}
    assert {
        c["creative_info"]["image_info"][0]["web_uri"] for c in ads[0]["creative_list"]
    } == {"cover-0", "cover-1"}
    with Session(db) as session:
        from app.modules.ads.models import (
            AdMaterialReference,
            AdObject,
            CampaignNameProjection,
        )
        from app.modules.ads.sync_models import AdDirectoryRun
        from app.modules.materials.cover_models import MaterialCoverJob

        assert not session.exec(select(MaterialCoverJob)).all()
        objects = session.exec(select(AdObject)).all()
        assert sorted(o.kind for o in objects) == ["ad", "ad", "adgroup", "campaign"]
        assert all(o.operation_status == "ENABLE" for o in objects)
        assert all(
            o.review_status is None and o.delivery_status is None for o in objects
        )
        assert all(o.source_channel == "OFFICIAL_API" for o in objects)
        campaign = next(o for o in objects if o.kind == "campaign")
        group_object = next(o for o in objects if o.kind == "adgroup")
        assert group_object.parent_remote_id == campaign.remote_id
        assert all(
            o.parent_remote_id == group_object.remote_id
            for o in objects
            if o.kind == "ad"
        )
        assert (
            session.exec(select(CampaignNameProjection)).one().raw_name == campaign.name
        )
        uses = session.exec(select(AdMaterialReference)).all()
        assert len(uses) == 4
        assert {u.platform_material_id for u in uses} == {"target-0", "target-1"}
        assert all(
            u.ad_material_id is None and u.operation_status is None for u in uses
        )
        assert all(u.complete for u in uses)
        assert all(u.local_material_id and u.name.startswith("Moon-") for u in uses)
        # 创建落库不能产生新的同步任务，也不能伪造平台同步水位。
        assert not session.exec(select(AdDirectoryRun)).all()
        assert all(
            session.get(ExecutionStep, id).status == "SUCCEEDED"
            for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"]
            for id in ids[kind]
        )
    # Redelivery after a lost ACK cannot invoke any create again.
    for kind in ["CTA", "CAMPAIGN", "ADGROUP", "AD"]:
        run(executable, redis_client, kind)
    assert len(calls) == 5


def test_lost_create_response_is_unknown_and_redelivery_never_recreates(
    executable, redis_client, monkeypatch
):
    db, context, ids = executable
    writes = []

    def request(_pool, method, _url, **_kwargs):
        writes.append(method)
        raise TimeoutError("synthetic response lost after remote persistence")

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    run(executable, redis_client, "CTA")
    run(executable, redis_client, "CTA")
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.status == "UNKNOWN" and step.request_body and step.remote_id is None
        assert session.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == step.id,
                StepEvidence.conclusion == "RESULT_UNKNOWN",
            )
        ).one()
    assert writes == ["POST"]


def test_revoked_between_admission_and_arm_prevents_any_sdk_call(
    executable, redis_client, monkeypatch
):
    from contextlib import contextmanager

    from app.modules.accounts.models import BCAccountAccess
    from app.modules.builds import execution

    db, context, ids = executable
    actual = execution.admitted_build_call

    @contextmanager
    def revoke(*args, **kwargs):
        with actual(*args, **kwargs):
            with Session(db) as session, session.begin():
                grant = session.exec(
                    select(BCAccountAccess).where(
                        BCAccountAccess.tenant_id == context.tenant_id
                    )
                ).one()
                grant.can_build = False
                session.add(grant)
            yield

    monkeypatch.setattr(execution, "admitted_build_call", revoke)

    def no_call(*_args, **_kwargs):
        raise AssertionError("SDK was called after permission revocation")

    monkeypatch.setattr("urllib3.PoolManager.request", no_call)
    run(executable, redis_client, "CTA")
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert (
            step.status == "FAILED"
            and step.request_body is None
            and step.remote_id is None
        )


def test_known_id_survives_sdk_cleanup_failure(executable, redis_client, monkeypatch):
    import urllib3

    db, _, ids = executable
    actual = urllib3.PoolManager.clear

    def cleanup_failure(pool):
        # 客户端关闭的真实资源边界必须先能读取已提交的远端 ID。
        with Session(db) as check:
            assert check.get(ExecutionStep, ids["CTA"][0]).remote_id == "known-id"
        actual(pool)
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(urllib3.PoolManager, "clear", cleanup_failure)
    calls = []

    def request(*_args, **_kwargs):
        calls.append(1)
        return HTTPResponse(
            body=b'{"code":0,"data":{"creative_portfolio_id":"known-id"},"request_id":"known"}',
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    run(executable, redis_client, "CTA")
    run(executable, redis_client, "CTA")
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.remote_id == "known-id" and step.status == "SUCCEEDED"
    assert len(calls) == 1


def test_lost_db_receipt_commit_never_recreates(executable, redis_client, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from app.modules.builds import execution

    db, _, ids = executable

    def lost_receipt(*_args, **_kwargs):
        raise SQLAlchemyError("synthetic database connection failure")

    monkeypatch.setattr(execution, "record_created", lost_receipt)
    writes = []

    def request(*_args, **_kwargs):
        writes.append(1)
        return HTTPResponse(
            body=b'{"code":0,"data":{"creative_portfolio_id":"created-once"},"request_id":"created"}',
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    run(executable, redis_client, "CTA")
    run(executable, redis_client, "CTA")
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.status == "UNKNOWN" and step.request_body and step.remote_id is None
    assert len(writes) == 1


def test_direct_execution_requires_real_bounded_prefork(context):
    from app.core.errors import DomainError
    from app.modules.builds.execution import process_step

    with pytest.raises(DomainError) as caught:
        process_step(
            database_engine=None,
            redis_client=None,
            context=context,
            step_id=uuid4(),
            revision=0,
        )
    assert caught.value.code == "build_worker_unbounded"


def test_official_json_number_conversion_cannot_round_frozen_decimal():
    from decimal import Decimal

    from app.core.errors import DomainError
    from app.modules.builds.frozen_requests import exact_number

    assert Decimal(str(exact_number(Decimal("100.25")))) == Decimal("100.25")
    with pytest.raises(DomainError):
        exact_number(Decimal("123456789012345678901234567.01"))
