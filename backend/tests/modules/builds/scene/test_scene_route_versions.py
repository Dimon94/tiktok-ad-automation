"""场景固定授权语义；凭据材料和目录观察更新不能改变原任务。"""

import inspect
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.builds.scene import _scope
from tests.modules.builds.scene.support import ensure


@pytest.mark.parametrize("name", ["read_scene_context", "ensure_scene_preparation"])
def test_scene_entry_requires_an_explicit_frozen_route(name):
    from app.modules.builds import scene, scene_jobs

    function = getattr(scene if name == "read_scene_context" else scene_jobs, name)
    parameter = inspect.signature(function).parameters.get("route")
    assert parameter is not None, "场景入口必须消费父冻结路由"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_scene_records_have_a_nullable_historical_route_column():
    from app.modules.builds.scene_job_models import DraftScenePreparation, SceneJob

    for model in (SceneJob, DraftScenePreparation):
        column = model.__table__.columns.get("frozen_route")
        assert column is not None, "任务必须持久化原冻结路由"
        assert column.nullable, "旧任务不能伪造今日授权"


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("change", ["credential", "observed", "default"])
def test_same_scene_basis_survives_nonsemantic_updates(
    database_engine, scene_case, change
):
    case = scene_case
    route = case["route"]
    kwargs = {
        "context": case["context"],
        "bc_id": route.bc_id,
        "advertiser_id": case["advertiser_id"],
        "link_id": case["link_id"],
        "route": route,
    }
    with Session(database_engine) as db:
        before = _scope(db, **kwargs)["basis"]
    with Session(database_engine) as db, db.begin():
        connection = db.get(TikTokConnection, route.connection_id)
        if change == "credential":
            connection.credential_revision += 1
        elif change == "observed":
            grant = db.get(
                BCAccountAccess,
                (
                    route.tenant_id,
                    route.bc_id,
                    case["advertiser_id"],
                    route.connection_id,
                ),
            )
            grant.checked_at = datetime.now(UTC)
            # 发现 run 的真实模型变化由完整 SceneJob 连续执行测试覆盖。
            authorization = db.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == route.connection_id
                )
            ).one()
            authorization.verified_at = datetime.now(UTC)
        else:
            # 删除默认后原route仍须可用；新任务没有默认将明确失败。
            db.delete(db.get(BCDefaultRoute, (route.tenant_id, route.bc_id)))
    with Session(database_engine) as db:
        assert _scope(db, **kwargs)["basis"] == before


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "change,code",
    [
        ("authorization", "route_authorization_changed"),
        ("contract", "route_contract_changed"),
    ],
)
def test_changed_semantics_reject_original_scene_scope(
    database_engine, scene_case, change, code
):
    case = scene_case
    with Session(database_engine) as db, db.begin():
        connection = db.get(TikTokConnection, case["route"].connection_id)
        if change == "authorization":
            connection.authorization_revision += 1
        else:
            connection.adapter_contract_revision = "changed-contract"
    with Session(database_engine) as db, pytest.raises(DomainError) as error:
        _scope(
            db,
            context=case["context"],
            bc_id=case["route"].bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=case["route"],
        )
    assert error.value.code == code


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_new_job_persists_the_exact_parent_route(database_engine, scene_case):
    from app.modules.builds.scene_job_models import SceneJob

    receipt = ensure(database_engine, scene_case)
    assert receipt.state == "queued", receipt.reason_code
    with Session(database_engine) as db:
        assert db.get(SceneJob, receipt.job_id).frozen_route == scene_case[
            "route"
        ].model_dump(mode="json")


def test_legacy_diagnostic_sender_is_removed():
    from app.modules.builds import scene

    assert not hasattr(scene, "refresh_scene_context")
    assert not hasattr(scene, "_scope_capabilities")


def test_scene_page_retains_actual_mcp_evidence_columns():
    from app.modules.builds.scene_job_models import SceneJobPage

    for name in ("mcp_request_id", "remote_task_id"):
        column = SceneJobPage.__table__.columns.get(name)
        assert column is not None
        assert column.nullable and column.type.length == 128


def test_scene_basis_contains_frozen_semantics_and_business_constraints():
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.builds import scene

    route = FrozenTikTokRoute(
        tenant_id=uuid4(),
        bc_id="bc",
        connection_id=uuid4(),
        channel="OFFICIAL_MCP",
        authorization_revision=2,
        adapter_contract_revision="contract",
    )
    business = {
        "advertiser_id": "account",
        "currency": "USD",
        "timezone": "UTC",
        "provider_id": "provider",
        "provider_version": 1,
        "provider_verification": "verified",
        "application_id": "app",
        "minis_id": "minis",
    }
    assert hasattr(scene, "scene_scope_basis"), "稳定摘要必须以冻结语义和业务依据为输入"
    basis = scene.scene_scope_basis(route=route, business=business)
    assert basis == scene.scene_scope_basis(
        route=route, business=dict(reversed(list(business.items())))
    )
    assert basis != scene.scene_scope_basis(
        route=route.model_copy(update={"authorization_revision": 3}), business=business
    )
    assert basis != scene.scene_scope_basis(
        route=route, business={**business, "minis_id": "other"}
    )


def test_draft_account_resolution_uses_its_explicit_connection_without_default(
    database_engine, scene_case
):
    from app.modules.accounts.resolver import resolve_lines
    from app.modules.accounts.schemas import InputLine

    case = scene_case
    route = case["route"]
    with Session(database_engine) as db, db.begin():
        db.delete(db.get(BCDefaultRoute, (route.tenant_id, route.bc_id)))
    with Session(database_engine) as db:
        result = resolve_lines(
            db,
            context=case["context"],
            bc_id=route.bc_id,
            connection_id=route.connection_id,
            lines=[InputLine(line_no=1, raw=case["advertiser_id"])],
        )
        assert result[0].status == "MATCHED"
        assert result[0].advertiser_id == case["advertiser_id"]


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_actual_scene_scope_versions_format_contract_and_distinguishes_rebinding(
    database_engine, scene_case
):
    from app.core.config import settings
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.builds import scene, scene_constraints

    case = scene_case
    current = case["route"]
    assert current.binding_revision == 0
    # 精确重建升级前的六字段存储，不使用新版 model_dump 作为旧摘要输入。
    stored = {
        "tenant_id": str(current.tenant_id),
        "bc_id": current.bc_id,
        "connection_id": str(current.connection_id),
        "channel": current.channel,
        "authorization_revision": current.authorization_revision,
        "adapter_contract_revision": current.adapter_contract_revision,
    }
    legacy = FrozenTikTokRoute.model_validate(stored)
    kwargs = {
        "context": case["context"],
        "bc_id": current.bc_id,
        "advertiser_id": case["advertiser_id"],
        "link_id": case["link_id"],
    }
    with Session(database_engine) as db:
        scope = _scope(db, route=legacy, **kwargs)
        access = scope["access"]
        historical = {
            "route": stored,
            "media_kind": "VIDEO",
            "compiler_contract_revision": scene.CREATIVE_COMPILER_REVISION,
            "scene_contract_revision": scene.SCENE_CONTRACT_REVISION,
            "constraints_revision": scene_constraints.REVISION,
            "max_age_seconds": settings.SCENE_MAX_AGE_SECONDS,
            "business": {
                "advertiser_id": case["advertiser_id"],
                "currency": access.currency,
                "timezone": access.timezone,
                "minis_id": scope["minis_id"],
            },
        }
        previous_digest = sha256(
            json.dumps(historical, sort_keys=True).encode()
        ).hexdigest()
        assert scope["basis"] == previous_digest
        assert _scope(db, route=current, **kwargs)["basis"] == previous_digest
    # 接入代数改变后，同一业务依据必须产生新摘要，不得重用旧场景证据。
    digests = {previous_digest}
    for revision in (1, 2):
        with Session(database_engine) as db, db.begin():
            binding = db.get(
                BCConnectionBinding,
                (current.tenant_id, current.bc_id, current.connection_id),
            )
            binding.revision = revision
            db.add(binding)
        rebound = current.model_copy(update={"binding_revision": revision})
        with Session(database_engine) as db:
            new_digest = _scope(db, route=rebound, **kwargs)["basis"]
        assert new_digest not in digests
        digests.add(new_digest)
    assert "binding_revision" not in stored
    assert legacy.binding_revision == current.binding_revision == 0


def test_scene_formats_and_compiler_versions_never_share_cache(
    database_engine, scene_case
):
    from types import SimpleNamespace

    from app.modules.builds.scene import _assemble_scene, scene_scope_basis
    from app.modules.builds.scene_job_models import SceneJob
    from app.modules.builds.scene_jobs import ensure_scene_preparation

    case = scene_case
    kwargs = {
        "context": case["context"],
        "bc_id": case["route"].bc_id,
        "advertiser_id": case["advertiser_id"],
        "link_id": case["link_id"],
        "route": case["route"],
    }
    with Session(database_engine) as db, db.begin():
        video = ensure_scene_preparation(db, **kwargs, media_kind="VIDEO")
        carousel = ensure_scene_preparation(db, **kwargs, media_kind="CAROUSEL")
        assert video.job_id != carousel.job_id
        assert db.get(SceneJob, carousel.job_id).media_kind == "CAROUSEL"
        scope = _scope(db, **kwargs, media_kind="CAROUSEL")
        assert scope["basis"] != _scope(db, **kwargs, media_kind="VIDEO")["basis"]
        scene = _assemble_scene(
            scope=scope,
            facts={
                "identity": {
                    "matches": [{"identity_id": "i", "identity_type": "TT_USER"}]
                }
            },
            reasons=[],
            evidence_ids=(),
            capability=SimpleNamespace(scope_verified=True, can_build=True),
            locally_operable=True,
        )
        assert not scene.supported
        assert "carousel_format_unverified" not in scene.reason_codes
        assert scene.creative_fields["creative_info"]["ad_format"] == "CAROUSEL_ADS"
    basis = scene_scope_basis(route=case["route"], business={}, media_kind="CAROUSEL")
    assert basis != scene_scope_basis(
        route=case["route"],
        business={},
        media_kind="CAROUSEL",
        compiler_contract_revision="next",
    )
