"""Public workspace prepare -> persistent SceneJob -> official SDK wire."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.builds.scene_job_models import SceneJob
from app.modules.builds.scene_jobs import ensure_scene_preparation, native_checkpoint
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene.test_scene_reads import wire as wire
from tests.modules.builds.scene_jobs import test_draft_bootstrap as bootstrap
from tests.modules.builds.scene_jobs.test_policy_regions import region_response
from tests.modules.builds.scene_jobs.test_service import (
    job_env as job_env,
)
from tests.modules.builds.scene_jobs.test_service import (
    role_page,
    run,
    run_capability,
)


@pytest.fixture
def source_env(monkeypatch, redis_client, isolated_strategy_database, request):
    import sys

    monkeypatch.setattr(sys.modules[__name__], "engine", isolated_strategy_database[0])
    yield from bootstrap.source_env.__wrapped__(
        monkeypatch, redis_client, isolated_strategy_database, request
    )


def selection():
    return {
        "kind": "NATIVE_SERIES_DIRECT_VIDEO",
        "advertiser_id": "actual-account",
        "native_series_id": "exact-series",
        "video_sources": [],
        "series_identity": {
            "identity_type": "TT_USER",
            "identity_id": "owner",
            "identity_authorized_bc_id": None,
        },
        "creative_identity": {
            "identity_type": "TT_USER",
            "identity_id": "owner",
            "identity_authorized_bc_id": None,
        },
        "regions": ["US"],
    }


def identities(rows, page=1, total=None):
    total = len(rows) if total is None else total
    return {
        "identity_list": rows,
        "page_info": {
            "page": page,
            "page_size": 50,
            "total_number": total,
            "total_page": (total + 49) // 50,
        },
    }


@pytest.mark.parametrize(
    "case", ["ready", "cursor", "permission", "missing_permission", "cross_identity"]
)
def test_native_job_exact_asset_complete_streams_and_unverified_combination(
    job_env, wire, redis_client, case
):
    bad_cursor = case == "cursor"
    env = job_env
    chosen = selection()
    if case == "cross_identity":
        chosen["creative_identity"]["identity_id"] = "other-0"
    with Session(engine, expire_on_commit=False) as session, session.begin():
        receipt = ensure_scene_preparation(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=None,
            route=env["route"],
            native_selection=chosen,
        )
    job = run(env, redis_client, receipt.job_id)
    wire[1].append(role_page(["actual-account"]))
    cap_env = {**env, "request_id": uuid4()}
    run_capability(cap_env, redis_client, job.capability_job_id)
    run_capability(cap_env, redis_client, job.capability_job_id)
    owner = {
        "identity_type": "TT_USER",
        "identity_id": "owner",
        "available_status": "AVAILABLE",
        "can_use_series": True,
        "can_push_video": True,
        "is_gpppa": False,
    }
    if case == "permission":
        owner["can_use_series"] = False
    if case == "missing_permission":
        owner.pop("can_push_video")
    rows = [owner] + [owner | {"identity_id": f"other-{i}"} for i in range(49)]
    wire[1].extend(
        [
            identities(rows, total=51),
            identities([owner | {"identity_id": "last"}], page=2, total=51),
            identities([]),
            {
                "list": [
                    {
                        "native_series_id": "wrong-series",
                        "native_series_name": "Same name",
                    }
                ],
                "cursor": 0 if bad_cursor else 17,
                "has_more": True,
            },
        ]
    )
    if not bad_cursor:
        wire[1].extend(
            [
                {
                    "list": [
                        {
                            "native_series_id": "exact-series",
                            "video_sources": [],
                            "native_series_name": "Same name",
                            "native_series_cover_url": "https://example.invalid/cover",
                        }
                    ],
                    "cursor": 17,
                    "has_more": False,
                },
                region_response()["data"],
            ]
        )
    for _ in range(8):
        job = run(env, redis_client, job.id)
        if job.status != "PENDING":
            break
    if bad_cursor:
        assert job.status == "FAILED" and job.error_code == "scene_response_unverified"
        return
    assert job.status == "COMPLETE", job.error_code
    with Session(engine, expire_on_commit=False) as session:
        draft = SimpleNamespace(scene_selection=chosen, bc_id=env["bc_id"], revision=7)
        result = native_checkpoint(
            session, context=env["context"], draft=draft, route=env["route"]
        )
        expected = {
            "ready": "READY",
            "permission": "BLOCKED",
            "missing_permission": "UNVERIFIED",
            "cross_identity": "UNVERIFIED",
        }[case]
        assert result.state == expected
        if case == "ready":
            assert result.reason_codes == []
        elif case == "permission":
            assert "native_series_identity_unavailable" in result.reason_codes
        else:
            assert (
                "native_series_identity_combination_unverified" in result.reason_codes
            )
        assert result.materials_ready is False and result.draft_revision == 7
        from app.modules.builds.scene import read_scene_context

        scene = read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=None,
            route=env["route"],
            native_selection=chosen,
            draft_revision=7,
        )
        assert scene.supported is (case == "ready")
        assert scene.adgroup_fields["native_series_id"] == "exact-series"
        assert scene.field_constraints["native_checkpoint"]["draft_revision"] == 7
        assert scene.field_constraints["native_checkpoint"]["route"] == env[
            "route"
        ].model_dump(mode="json")

        assert len(result.evidence_ids) == 6
        assert len(result.facts["native_tt_identity"]["items"]) == 51
        assert (
            result.facts["native_series"]["items"][1]["native_series_id"]
            == "exact-series"
        )
        from datetime import UTC, datetime, timedelta

        if case == "ready":
            from app.modules.accounts.capability_models import CapabilityJob

            capability = session.get(CapabilityJob, job.capability_job_id)
            original_expiry = capability.expires_at
            capability.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.flush()
            assert native_checkpoint(
                session, context=env["context"], draft=draft, route=env["route"]
            ).reason_codes == ["account_build_unverified"]
            assert (
                ensure_scene_preparation(
                    session,
                    context=env["context"],
                    bc_id=env["bc_id"],
                    advertiser_id="actual-account",
                    link_id=None,
                    route=env["route"],
                    native_selection=chosen,
                ).state
                == "queued"
            )
            capability.expires_at = original_expiry
            session.flush()
        persisted = session.get(SceneJob, job.id)
        persisted.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()
        assert native_checkpoint(
            session, context=env["context"], draft=draft, route=env["route"]
        ).reason_codes == ["scene_evidence_expired"]
        changed = selection() | {"native_series_id": "wrong-series"}
        draft.scene_selection = changed
        assert (
            native_checkpoint(
                session, context=env["context"], draft=draft, route=env["route"]
            ).state
            == "PENDING"
        )
    series_calls = [
        dict(call[2]["fields"]) for call in wire[0] if "native_series/get" in call[1]
    ]
    assert [call["cursor"] for call in series_calls] == [0, 17]
    assert all(call["count"] == 20 and "page_size" not in call for call in series_calls)
    identity_calls = [
        dict(call[2]["fields"]) for call in wire[0] if "/identity/get" in call[1]
    ]
    assert [(call["identity_type"], call["page"]) for call in identity_calls] == [
        ("TT_USER", 1),
        ("TT_USER", 2),
        ("BC_AUTH_TT", 1),
    ]


def prepared_native_workspace(job_env, wire, redis_client, materials, count=2):
    from app.modules.builds import drafts
    from app.modules.builds.catalog import draft_summary
    from app.modules.strategies.models import StrategyVersion
    from app.modules.strategies.service import create_strategy
    from tests.modules.strategies.test_versions import config

    env = job_env
    chosen = selection()
    expected = []
    with Session(engine, expire_on_commit=False) as session, session.begin():
        if materials:
            from datetime import UTC, datetime

            from app.modules.materials.models import AccountMaterial, MaterialFile

            original = session.get(MaterialFile, env["material_id"])
            for index in range(count):
                values = original.model_dump()
                values.update(
                    id=uuid4(),
                    object_key=f"native/{uuid4()}",
                    video_md5=str(index + 1) * 32 if index < 2 else f"{index + 1:032x}",
                )
                material = MaterialFile(**values)
                session.add(material)
                session.flush()
                session.add(
                    AccountMaterial(
                        tenant_id=env["context"].tenant_id,
                        bc_id=env["bc_id"],
                        material_id=material.id,
                        advertiser_id="actual-account",
                        connection_id=env["connection_id"],
                        frozen_route=env["route"].model_dump(mode="json"),
                        video_id=f"target-{index}",
                        image_id=f"cover-{index}",
                        status="available",
                        verified_at=datetime.now(UTC),
                    )
                )
                chosen["video_sources"].append({"material_id": str(material.id)})
                expected.append((str(material.id), f"target-{index}", f"cover-{index}"))
        strategy_id = create_strategy(
            session, context=env["context"], name="Native workspace", config=config()
        )
        version = session.exec(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
        ).one()
        draft_id = drafts.create_draft(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            strategy_version_id=version.id,
            provider_connection_id=None,
            application_id=None,
            drama_lines=[],
            account_lines=["actual-account"],
            link_config={},
            scene_selection=chosen,
        )
        prep_id = drafts.prepare_draft(
            session, context=env["context"], draft_id=draft_id, request_id=uuid4()
        )
    # Drive the existing workspace continuation. Capability reads use the same SDK wire.
    for _ in range(8):
        with Session(engine, expire_on_commit=False) as session, session.begin():
            drafts.continue_draft(session, context=env["context"], task_id=prep_id)
            from app.modules.accounts.capability_models import CapabilityJob

            pending = session.exec(
                select(CapabilityJob).where(
                    CapabilityJob.tenant_id == env["context"].tenant_id,
                    CapabilityJob.status == "PENDING",
                )
            ).first()
            native_job = session.exec(
                select(SceneJob).where(
                    SceneJob.tenant_id == env["context"].tenant_id,
                    SceneJob.native_selection.is_not(None),
                )
            ).first()
        if pending:
            wire[1].append(role_page(["actual-account"]))
            run_capability({**env, "request_id": uuid4()}, redis_client, pending.id)
            run_capability({**env, "request_id": uuid4()}, redis_client, pending.id)
        if native_job:
            break
    assert native_job is not None
    owner = {
        "identity_type": "TT_USER",
        "identity_id": "owner",
        "available_status": "AVAILABLE",
        "can_use_series": True,
        "can_push_video": True,
        "is_gpppa": False,
    }
    wire[1].extend(
        [
            identities([owner]),
            identities([]),
            {
                "list": [{"native_series_id": "exact-series"}],
                "cursor": 0,
                "has_more": False,
            },
            region_response()["data"],
        ]
    )
    for _ in range(6):
        native_job = run(env, redis_client, native_job.id)
        if native_job.status != "PENDING":
            break
    assert native_job.status == "COMPLETE", native_job.error_code
    with Session(engine, expire_on_commit=False) as session, session.begin():
        for _ in range(51):
            if drafts.continue_draft(session, context=env["context"], task_id=prep_id):
                break
        result = draft_summary(session, context=env["context"], draft_id=draft_id)
        assert result.status == ("READY" if materials else "BLOCKED")
        assert result.error_code == (
            None if materials else "native_materials_not_prepared"
        )
        if materials:
            from app.modules.builds.models import BuildDraft
            from app.modules.builds.scene_jobs import native_checkpoint

            checkpoint = native_checkpoint(
                session,
                context=env["context"],
                draft=session.get(BuildDraft, draft_id),
                route=env["route"],
            )
            assert checkpoint.materials_ready
            assert [
                (str(item.material_id), item.video_id, item.image_id)
                for item in checkpoint.materials
            ] == expected
            assert all(
                item.video_evidence_id and item.cover_evidence_id
                for item in checkpoint.materials
            )
        assert result.provider_task_id is None
        assert (
            result.scene_selection.series_identity
            == result.scene_selection.creative_identity
        )
    assert not any("minis/get" in call[1] for call in wire[0])
    return draft_id, prep_id, expected


@pytest.mark.parametrize("materials", [False, True])
def test_workspace_prepare_drives_native_job_without_provider(
    job_env, wire, redis_client, materials
):
    prepared_native_workspace(job_env, wire, redis_client, materials)


@pytest.mark.parametrize(
    "payload,valid",
    [
        ({"list": [], "cursor": 0, "has_more": False}, True),
        (
            {
                "list": [{"native_series_id": "same"}, {"native_series_id": "same"}],
                "cursor": 0,
                "has_more": False,
            },
            False,
        ),
        ({"list": [], "cursor": 0, "has_more": True}, False),
        ({"list": [], "cursor": 17}, False),
    ],
)
def test_native_series_sdk_empty_duplicate_and_incomplete_pages(wire, payload, valid):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    import business_api_client as sdk

    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.accounts import RuntimeReadContext
    from app.integrations.tiktok.official.scenes import OfficialScenesGateway

    @contextmanager
    def scope(_advertiser_id, _operation, _deadline):
        yield "offline-token"

    client = sdk.ApiClient()
    client.default_headers["Access-Token"] = "offline-token"
    wire[1].append(payload)
    gateway = OfficialScenesGateway(
        client,
        context=RuntimeReadContext(bc_id="bc-test"),
        request_scope=scope,
        deadline=datetime.now(UTC) + timedelta(seconds=20),
    )
    try:
        if valid:
            page = gateway.read_page(
                resource="native_series",
                advertiser_id="actual-account",
                page=1,
                minis_id=None,
                native_selection=selection(),
            )
            assert page.last is True and page.facts.items == ()
        else:
            with pytest.raises(DomainError) as error:
                gateway.read_page(
                    resource="native_series",
                    advertiser_id="actual-account",
                    page=1,
                    minis_id=None,
                    native_selection=selection(),
                )
            assert error.value.code == "scene_response_unverified"
    finally:
        client.rest_client.pool_manager.clear()


def test_native_pending_selection_discovers_identities_without_fake_series(
    job_env, wire, redis_client
):
    env = job_env
    pending = selection() | {
        "native_series_id": None,
        "series_identity": None,
        "creative_identity": None,
    }
    with Session(engine) as session, session.begin():
        receipt = ensure_scene_preparation(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=None,
            route=env["route"],
            native_selection=pending,
        )
    job = run(env, redis_client, receipt.job_id)
    wire[1].append(role_page(["actual-account"]))
    run_capability({**env, "request_id": uuid4()}, redis_client, job.capability_job_id)
    run_capability({**env, "request_id": uuid4()}, redis_client, job.capability_job_id)
    wire[1].extend(
        [
            identities(
                [
                    {
                        "identity_type": "TT_USER",
                        "identity_id": "actual-owner",
                        "can_use_series": True,
                    }
                ]
            ),
            identities([]),
            region_response()["data"],
        ]
    )
    for _ in range(4):
        job = run(env, redis_client, job.id)
        if job.status != "PENDING":
            break
    assert job.status == "COMPLETE", job.error_code
    with Session(engine) as session:
        checkpoint = native_checkpoint(
            session,
            context=env["context"],
            draft=SimpleNamespace(
                scene_selection=pending, bc_id=env["bc_id"], revision=1
            ),
            route=env["route"],
        )
        assert checkpoint.state == "BLOCKED"
        assert "native_series_identity_selection_required" in checkpoint.reason_codes
        assert (
            checkpoint.facts["native_tt_identity"]["items"][0]["identity_id"]
            == "actual-owner"
        )
    assert not any("native_series/get" in call[1] for call in wire[0])


@pytest.mark.parametrize(
    "fault",
    [
        "missing_video",
        "missing_cover",
        "unknown",
        "wrong_account",
        "order",
        "duplicate",
        "evidence",
        "scope",
        "old_revision",
        "digest",
        "count",
    ],
)
def test_native_material_group_rejects_partial_stale_or_corrupt_evidence(
    job_env, wire, redis_client, fault
):
    from app.modules.builds.models import BuildDraft, DraftPreparation
    from app.modules.builds.scene_job_models import DraftScenePreparation
    from app.modules.materials.models import AccountMaterial

    draft_id, prep_id, expected = prepared_native_workspace(
        job_env, wire, redis_client, True
    )
    with Session(engine, expire_on_commit=False) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        state = session.get(
            DraftScenePreparation, (job_env["context"].tenant_id, prep_id)
        )
        import copy

        proof = copy.deepcopy(state.native_materials)
        mapping = session.get(AccountMaterial, proof["items"][1]["video_evidence_id"])
        if fault == "count":
            proof["items"].pop()
        elif fault == "missing_video":
            mapping.status = "unavailable"
        elif fault == "missing_cover":
            mapping.image_id = None
        elif fault == "unknown":
            mapping.status = "result_unknown"
        elif fault == "wrong_account":
            from tests.modules.materials.test_readiness import target

            account = target(session, job_env)
            wrong = AccountMaterial(
                **{**mapping.model_dump(), "id": uuid4(), "advertiser_id": account}
            )
            session.add(wrong)
            session.flush()
            proof["items"][1]["video_evidence_id"] = str(wrong.id)
        elif fault == "order":
            proof["items"].reverse()
        elif fault == "duplicate":
            proof["items"][1] = proof["items"][0]
        elif fault == "evidence":
            proof["items"][1].pop("cover_evidence_id")
        elif fault == "scope":
            proof["scope_basis"] = "old-route"
        elif fault == "old_revision":
            draft.revision += 1
            draft.status = "DRAFT"
        elif fault == "digest":
            from app.modules.materials.models import MaterialFile

            session.get(MaterialFile, mapping.material_id).video_md5 = "f" * 32
        state.native_materials = proof
        session.flush()
        checkpoint = native_checkpoint(
            session, context=job_env["context"], draft=draft, route=job_env["route"]
        )
        assert checkpoint.materials_ready is False
        assert checkpoint.materials == []
        if fault != "old_revision":
            assert checkpoint.material_issues
        else:
            # 重复旧消息只能使原任务过期，不能重写新草稿或覆盖新选择。
            from app.modules.builds.drafts import continue_draft

            session.get(DraftPreparation, prep_id).status = "PENDING"
            assert continue_draft(session, context=job_env["context"], task_id=prep_id)
            assert session.get(DraftPreparation, prep_id).status == "OBSOLETE"
            assert draft.status == "DRAFT"


def test_native_cover_worker_continuation_and_duplicate_prepare(
    job_env, wire, redis_client
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.modules.builds import drafts
    from app.modules.builds.models import BuildDraft, DraftPreparation
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.covers import run_cover
    from app.modules.materials.models import AccountMaterial

    draft_id, prep_id, expected = prepared_native_workspace(
        job_env, wire, redis_client, True
    )
    with Session(engine) as session, session.begin():
        mapping = session.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == expected[1][0])
        ).one()
        mapping.image_id = None
        from app.modules.materials.covers import ensure_source_cover

        # 已上传来源的成功事件沿既有 owner 排队；后续 BUILD 不创建第二个任务。
        ensure_source_cover(
            session,
            context=job_env["context"],
            bc_id=job_env["bc_id"],
            material_id=mapping.material_id,
            advertiser_id="actual-account",
            task_key="source-upload-event",
            route=job_env["route"],
        )
        prep = session.get(DraftPreparation, prep_id)
        prep.status, prep.phase = "PENDING", "scenes"
        session.get(BuildDraft, draft_id).status = "PREPARING"
    barrier = Barrier(2)

    def resume():
        barrier.wait(10)
        with Session(engine) as session, session.begin():
            return drafts.continue_draft(
                session, context=job_env["context"], task_id=prep_id
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(resume), pool.submit(resume)
        assert a.result(15) is False and b.result(15) is False
    with Session(engine) as session:
        jobs = session.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == job_env["context"].tenant_id
            )
        ).all()
        assert len(jobs) == 1
        job = jobs[0]
        cp = native_checkpoint(
            session,
            context=job_env["context"],
            draft=session.get(BuildDraft, draft_id),
            route=job_env["route"],
        )
        assert not cp.materials_ready and cp.materials == []
        assert cp.material_issues[0]["material_id"] == expected[1][0]
    wire[1].extend(
        [
            {
                "list": [
                    {
                        "video_id": "target-1",
                        "displayable": True,
                        "signature": "2" * 32,
                        "width": 720,
                        "height": 1280,
                        "video_cover_url": "https://cover.example.invalid/a.jpg",
                    }
                ]
            },
            {
                "advertiser_id": "actual-account",
                "image_id": "worker-cover",
                "material_id": "1234567890123456789",
                "signature": "c" * 32,
                "width": 360,
                "height": 640,
                "displayable": False,
            },
        ]
    )
    run_cover(
        database_engine=engine,
        redis_client=redis_client,
        context=job_env["context"],
        job_id=job.id,
        dispatch_id=job.dispatch_id,
        revision=job.revision,
        read=False,
    )
    with Session(engine) as session, session.begin():
        current = session.get(MaterialCoverJob, job.id)
        assert current.status == "READY", current.error_code
        assert drafts.continue_draft(
            session, context=job_env["context"], task_id=prep_id
        )
        cp = native_checkpoint(
            session,
            context=job_env["context"],
            draft=session.get(BuildDraft, draft_id),
            route=job_env["route"],
        )
        assert cp.materials_ready
        assert [item.video_id for item in cp.materials] == ["target-0", "target-1"]
        assert cp.materials[1].image_id == "worker-cover"
        assert cp.materials[1].cover_task_id == job.id
        # UNKNOWN 回执永远不能凭旧映射图片继续通过。
        current.status = "UNKNOWN"
        session.flush()
        assert not native_checkpoint(
            session,
            context=job_env["context"],
            draft=session.get(BuildDraft, draft_id),
            route=job_env["route"],
        ).materials_ready


def test_native_selected_image_cover_and_shared_source_are_exact(
    job_env, wire, redis_client
):
    from datetime import UTC, datetime

    from app.modules.builds.drafts import continue_draft
    from app.modules.builds.models import BuildDraft, DraftPreparation
    from app.modules.materials.models import AccountMaterial, MaterialFile

    draft_id, prep_id, expected = prepared_native_workspace(
        job_env, wire, redis_client, True
    )
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        original = session.get(MaterialFile, expected[0][0])
        image = MaterialFile(
            **{
                **original.model_dump(),
                "id": uuid4(),
                "object_key": f"cover/{uuid4()}",
                "media_type": "IMAGE",
                "mime_type": "image/jpeg",
                "duration": None,
            }
        )
        session.add(image)
        session.flush()
        mapping = AccountMaterial(
            tenant_id=job_env["context"].tenant_id,
            bc_id=job_env["bc_id"],
            material_id=image.id,
            advertiser_id="actual-account",
            connection_id=job_env["connection_id"],
            frozen_route=job_env["route"].model_dump(mode="json"),
            media_type="IMAGE",
            video_id=None,
            image_id="explicit-selected-cover",
            is_carousel_usable=True,
            status="available",
            verified_at=datetime.now(UTC),
        )
        session.add(mapping)
        import copy

        chosen = copy.deepcopy(draft.scene_selection)
        chosen["video_sources"][0]["cover_material_id"] = str(image.id)
        # 同一素材重复用于两个创意位置仍保持N项，同一目标映射，没有新增上传。
        chosen["video_sources"].append(copy.deepcopy(chosen["video_sources"][0]))
        from app.modules.builds.drafts import prepare_draft, update_draft

        update_draft(
            session,
            context=job_env["context"],
            draft_id=draft_id,
            expected_revision=draft.revision,
            scene_selection=chosen,
        )
        prep_id = prepare_draft(
            session, context=job_env["context"], draft_id=draft_id, request_id=uuid4()
        )
        prep = session.get(DraftPreparation, prep_id)
        # 账户能力已由第一轮官方SDK准备，这里接续当前目的地场景。
        prep.phase = "scenes"
        session.flush()
    from app.modules.builds.scene_jobs import ensure_scene_preparation

    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        receipt = ensure_scene_preparation(
            session,
            context=job_env["context"],
            bc_id=job_env["bc_id"],
            advertiser_id="actual-account",
            link_id=None,
            route=job_env["route"],
            native_selection=draft.scene_selection,
        )
    owner = {
        "identity_type": "TT_USER",
        "identity_id": "owner",
        "available_status": "AVAILABLE",
        "can_use_series": True,
        "can_push_video": True,
        "is_gpppa": False,
    }
    wire[1].extend(
        [
            identities([owner]),
            identities([]),
            {
                "list": [{"native_series_id": "exact-series"}],
                "cursor": 0,
                "has_more": False,
            },
            region_response()["data"],
        ]
    )
    for _ in range(8):
        job = run(job_env, redis_client, receipt.job_id)
        if job.status != "PENDING":
            break
    with Session(engine) as session, session.begin():
        assert continue_draft(session, context=job_env["context"], task_id=prep_id)
        draft = session.get(BuildDraft, draft_id)
        cp = native_checkpoint(
            session, context=job_env["context"], draft=draft, route=job_env["route"]
        )
        assert cp.materials_ready, cp.material_issues
        assert [item.video_id for item in cp.materials] == [
            "target-0",
            "target-1",
            "target-0",
        ]
        assert (
            cp.materials[0].image_id
            == cp.materials[2].image_id
            == "explicit-selected-cover"
        )
        assert cp.materials[0].video_evidence_id == cp.materials[2].video_evidence_id


def test_native_video_verification_resumes_same_distribution(
    job_env, wire, redis_client
):
    from app.modules.builds import drafts
    from app.modules.builds.models import BuildDraft, DraftPreparation
    from app.modules.materials.distribution import run_distribution
    from app.modules.materials.models import AccountMaterial, MaterialDistribution

    draft_id, prep_id, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    with Session(engine, expire_on_commit=False) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        source_id = draft.scene_selection["video_sources"][1]["material_id"]
        mapping = session.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == source_id)
        ).one()
        mapping.status = "result_unknown"
        prep = session.get(DraftPreparation, prep_id)
        prep.status, prep.phase = "PENDING", "scenes"
        assert not drafts.continue_draft(
            session, context=job_env["context"], task_id=prep_id
        )
        first = session.exec(
            select(MaterialDistribution).where(
                MaterialDistribution.material_id == source_id
            )
        ).one()
        assert not drafts.continue_draft(
            session, context=job_env["context"], task_id=prep_id
        )
        assert (
            len(
                session.exec(
                    select(MaterialDistribution).where(
                        MaterialDistribution.material_id == source_id
                    )
                ).all()
            )
            == 1
        )
    wire[1].append(
        {"list": [{"video_id": "target-1", "signature": "2" * 32, "displayable": True}]}
    )
    run_distribution(
        database_engine=engine,
        redis_client=redis_client,
        context=job_env["context"],
        distribution_id=first.id,
        kind="verify",
    )
    with Session(engine, expire_on_commit=False) as session, session.begin():
        assert session.get(MaterialDistribution, first.id).status == "ready"
        assert drafts.continue_draft(
            session, context=job_env["context"], task_id=prep_id
        )
        cp = native_checkpoint(
            session,
            context=job_env["context"],
            draft=session.get(BuildDraft, draft_id),
            route=job_env["route"],
        )
        assert cp.materials_ready, cp.material_issues
        assert cp.materials[1].video_task_id == first.id


def test_native_materials_span_multiple_bounded_turns_without_truncation(
    job_env, wire, redis_client
):
    prepared_native_workspace(job_env, wire, redis_client, True, count=21)


def test_native_multiconnection_cover_ambiguity_cannot_reuse_ready_mapping(
    job_env, wire, redis_client
):
    from datetime import UTC, datetime

    from app.modules.accounts.routing import freeze_route
    from app.modules.builds.drafts import continue_draft
    from app.modules.builds.models import BuildDraft, DraftPreparation
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.models import AccountMaterial
    from tests.modules.materials.route_support import second_connection

    draft_id, prep_id, expected = prepared_native_workspace(
        job_env, wire, redis_client, True
    )
    with Session(engine) as session, session.begin():
        replacement = second_connection(session, job_env)
        mapping = session.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == expected[1][0])
        ).one()
        for connection in [job_env["connection_id"], replacement]:
            route = freeze_route(
                session,
                context=job_env["context"],
                bc_id=job_env["bc_id"],
                connection_id=connection,
            )
            session.add(
                MaterialCoverJob(
                    tenant_id=job_env["context"].tenant_id,
                    bc_id=job_env["bc_id"],
                    material_id=mapping.material_id,
                    asset_id=mapping.id,
                    advertiser_id="actual-account",
                    connection_id=connection,
                    actor_id=job_env["context"].actor_id,
                    frozen_route=route.model_dump(mode="json"),
                    video_id="target-1",
                    video_md5="2" * 32,
                    remote_name="fixture-cover.jpg",
                    status="READY",
                    request_armed_at=datetime.now(UTC),
                    known_image_id="cover-1",
                )
            )
        session.flush()
        draft = session.get(BuildDraft, draft_id)
        cp = native_checkpoint(
            session, context=job_env["context"], draft=draft, route=job_env["route"]
        )
        assert not cp.materials_ready and cp.materials == []
        assert cp.material_issues[0]["reason_code"] == "native_cover_mapping_ambiguous"
        prep = session.get(DraftPreparation, prep_id)
        prep.status, prep.phase = "PENDING", "scenes"
        assert continue_draft(session, context=job_env["context"], task_id=prep_id)
        assert prep.status == draft.status == "BLOCKED"
        assert prep.error_code == "native_cover_mapping_ambiguous"
