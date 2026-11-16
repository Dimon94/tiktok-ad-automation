# ruff: noqa: F811
"""预设是不可变版本；载入是一笔受 CAS 保护的独立创意编辑。"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.image_composition_commands import SetMusic
from app.modules.builds.image_composition_service import (
    edit_creative_units,
    list_creative_units,
)
from app.modules.materials import carousel_presets as presets
from tests.modules.builds.test_image_composition_service import (  # noqa: F401
    apply_rule,
    image_draft,
)


def save(session, case, source=None, name="Moon preset"):
    draft, _, scope = case
    unit = source or list_creative_units(session, scope).items[0]
    return presets.save_preset_version(
        session, scope, unit["id"], draft.revision, name, uuid4()
    )


def test_versions_copy_and_loaded_identity_are_independent(session, image_draft):
    draft, drama, scope = image_draft
    source = apply_rule(session, image_draft)[0]
    edit_creative_units(
        session,
        scope,
        uuid4(),
        draft.revision,
        SetMusic(kind="SET_MUSIC", unit_id=source["id"], music_id="song-a"),
    )
    first = save(session, image_draft)
    revision = draft.revision
    loaded = presets.load_preset_into_draft(
        session,
        scope,
        first["id"],
        uuid4(),
        revision,
        drama_id=drama.drama_id,
        allow_reuse=True,
    )
    assert draft.revision == revision + 1
    assert loaded["unit"]["id"] != str(source["id"])
    assert loaded["unit"]["composition_protected"] and loaded["unit"]["music_protected"]
    assert loaded["unit"]["music_id"] == "song-a"
    edit_creative_units(
        session,
        scope,
        uuid4(),
        draft.revision,
        SetMusic(
            kind="SET_MUSIC", unit_id=UUID(loaded["unit"]["id"]), music_id="song-b"
        ),
    )
    second = save(session, image_draft, source={"id": UUID(loaded["unit"]["id"])})
    copied = presets.copy_preset_version(
        session, scope.context, first["id"], "Copied", uuid4()
    )
    assert second["version"] == 2 and second["music_id"] == "song-b"
    assert copied["id"] != first["id"] and copied["music_id"] == "song-a"
    assert presets.get_preset_version(session, scope.context, first["id"]) == first
    assert (
        presets.list_preset_versions(session, scope.context, name="Moon")["total"] == 2
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text("UPDATE carousel_preset_version SET name='mutated' WHERE id=:id"),
            {"id": first["id"]},
        )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text("DELETE FROM carousel_preset_item WHERE version_id=:id"),
            {"id": first["id"]},
        )


def test_load_replay_pending_strip_intent_and_real_preparation(session, image_draft):
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.image_composition_models import DraftCreativeUnit

    draft, drama, scope = image_draft
    source = apply_rule(session, image_draft)[0]
    row = session.get(
        DraftCreativeUnit,
        (scope.tenant_id, draft.id, draft.active_image_generation, source["id"]),
    )
    row.music_snapshot = {
        "pending_request": {"request_id": str(uuid4()), "action": "RECOMMEND"},
        "preview_url": "https://secret",
        "display_name": "unselected",
    }
    version = save(session, image_draft)
    assert version["music_snapshot"] == {} and version["music_id"] is None
    request, revision = uuid4(), draft.revision
    result = presets.load_preset_into_draft(
        session,
        scope,
        version["id"],
        request,
        revision,
        drama_id=drama.drama_id,
        allow_reuse=True,
    )
    assert result["unit"]["music_status"] == "PENDING"
    assert not result["unit"]["music_protected"]
    assert result == presets.load_preset_into_draft(
        session,
        scope,
        version["id"],
        request,
        revision,
        drama_id=drama.drama_id,
        allow_reuse=True,
    )
    assert session.exec(
        select(MusicPreparationRun).where(MusicPreparationRun.draft_id == draft.id)
    ).all()
    with pytest.raises(DomainError) as error:
        presets.load_preset_into_draft(
            session,
            scope,
            version["id"],
            request,
            revision,
            drama_id=drama.drama_id,
            allow_reuse=False,
        )
    assert error.value.code == "idempotency_conflict"


def test_pending_old_selection_cannot_become_ready_preset(session, image_draft):
    from app.modules.builds.image_composition_models import DraftCreativeUnit

    draft, _, scope = image_draft
    source = apply_rule(session, image_draft)[0]
    row = session.get(
        DraftCreativeUnit,
        (scope.tenant_id, draft.id, draft.active_image_generation, source["id"]),
    )
    row.music_status, row.music_id = "PENDING", "old-song"
    row.music_snapshot = {
        "music_id": "old-song",
        "pending_request": {"request_id": str(uuid4())},
        "preview_url": "https://old",
    }
    version = save(session, image_draft)
    assert version["music_status"] == "PENDING" and version["music_id"] is None


def test_load_reserves_images_and_never_silently_restores_exclusions(
    session, image_draft
):
    from app.modules.builds.image_composition_service import exclude_candidates

    draft, drama, scope = image_draft
    source = apply_rule(session, image_draft)[0]
    version = save(session, image_draft)
    revision = draft.revision
    with pytest.raises(DomainError) as error:
        presets.load_preset_into_draft(
            session, scope, version["id"], uuid4(), revision, drama_id=drama.drama_id
        )
    assert error.value.code == "image_reuse_confirmation_required"
    loaded = presets.load_preset_into_draft(
        session,
        scope,
        version["id"],
        uuid4(),
        revision,
        drama_id=drama.drama_id,
        allow_reuse=True,
    )
    # 明确复用只增加独立单元，不偷偷裁剪已有自动组合。
    assert (
        list_creative_units(session, scope).items[0]["ordered_material_ids"]
        == source["ordered_material_ids"]
    )
    units = apply_rule(session, image_draft)
    assert any(str(u["id"]) == loaded["unit"]["id"] for u in units)
    members = [str(i) for u in units for i in u["ordered_material_ids"]]
    assert all(members.count(i) == 1 for i in version["ordered_material_ids"])
    exclude_candidates(
        session,
        scope,
        uuid4(),
        draft.revision,
        drama.drama_id,
        [UUID(version["ordered_material_ids"][0])],
    )
    revision = draft.revision
    with pytest.raises(DomainError) as error:
        presets.load_preset_into_draft(
            session,
            scope,
            version["id"],
            uuid4(),
            revision,
            drama_id=drama.drama_id,
            allow_reuse=True,
        )
    assert error.value.code == "image_candidate_unavailable"
    assert draft.revision == revision


def destination(session, case, *, bc=None):
    from app.modules.builds.image_composition_service import resolve_image_scope
    from app.modules.builds.models import BuildDraft, DraftDrama, DraftInput

    draft, drama, scope = case
    copy = BuildDraft(
        **{
            **draft.model_dump(),
            "id": uuid4(),
            "request_id": uuid4(),
            "bc_id": bc or draft.bc_id,
            "active_image_generation": 0,
            "revision": 1,
            "applied_image_rule_revision": None,
        }
    )
    session.add(copy)
    session.flush()
    target_drama = DraftDrama(
        **{
            **drama.model_dump(),
            "draft_id": copy.id,
            "bc_id": copy.bc_id,
            "matched_count": 0,
        }
    )
    session.add(target_drama)
    session.add(
        DraftInput(
            tenant_id=scope.tenant_id,
            draft_id=copy.id,
            kind="drama",
            line_no=1,
            raw_text=drama.title,
            drama_id=drama.drama_id,
        )
    )
    session.flush()
    return (
        copy,
        target_drama,
        resolve_image_scope(session, context=scope.context, draft_id=copy.id),
    )


def test_load_missing_candidates_is_one_revision_and_new_draft_independent(
    session, image_draft
):
    from app.modules.builds.carousel_music_models import (
        MusicPreparationRun,
        MusicSelectionJob,
    )
    from app.modules.builds.carousel_music_tasks import advance_music_preparation
    from app.modules.builds.image_composition_service import list_candidates
    from app.modules.builds.image_composition_state import ImageCompositionRequest

    apply_rule(session, image_draft)
    version = save(session, image_draft)
    draft, drama, scope = destination(session, image_draft)
    result = presets.load_preset_into_draft(
        session, scope, version["id"], uuid4(), 1, drama_id=drama.drama_id
    )
    assert result["revision"] == 2 and draft.revision == 2
    assert list_candidates(session, scope).total == 3
    receipts = session.exec(
        select(ImageCompositionRequest).where(
            ImageCompositionRequest.draft_id == draft.id
        )
    ).all()
    assert len(receipts) == 1
    scan = session.exec(
        select(MusicPreparationRun).where(MusicPreparationRun.draft_id == draft.id)
    ).one()
    assert scan.dispatch_id is not None
    advance_music_preparation(session, scan.id, scan.generation)
    jobs = session.exec(
        select(MusicSelectionJob).where(MusicSelectionJob.draft_id == draft.id)
    ).all()
    assert len(jobs) == 1 and jobs[0].dispatch_id is not None
    assert jobs[0].targets[0]["unit_id"] == result["unit"]["id"]
    assert "pending_request" not in result["unit"]["music_snapshot"]


def test_scope_legal_source_and_cross_bc_audit(session, image_draft, other_context):
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import TenantBC, TikTokConnection
    from app.modules.materials.models import MaterialFile

    apply_rule(session, image_draft)
    version = save(session, image_draft)
    with pytest.raises(DomainError) as error:
        presets.get_preset_version(session, other_context, version["id"])
    assert error.value.code == "carousel_preset_not_found"
    assert presets.list_preset_versions(session, other_context)["total"] == 0
    source_scope = image_draft[2]
    session.add(TenantBC(tenant_id=source_scope.tenant_id, bc_id="preset-target"))
    session.flush()
    connection = session.get(TikTokConnection, source_scope.route.connection_id)
    session.add(
        BCConnectionBinding(
            tenant_id=source_scope.tenant_id,
            bc_id="preset-target",
            connection_id=connection.id,
            kind=connection.kind,
        )
    )
    session.flush()
    session.add(
        BCDefaultRoute(
            tenant_id=source_scope.tenant_id,
            bc_id="preset-target",
            connection_id=connection.id,
        )
    )
    session.flush()
    draft, drama, scope = destination(session, image_draft, bc="preset-target")
    loaded = presets.load_preset_into_draft(
        session, scope, version["id"], uuid4(), draft.revision, drama_id=drama.drama_id
    )
    assert loaded["unit"]["music_status"] == "PENDING"
    assert (
        presets.get_preset_version(session, scope.context, version["id"])[
            "source_bc_id"
        ]
        == source_scope.bc_id
    )
    # 原件失效且目标没有真实合法映射，不能凭原 BC 或预设引用猜可用来源。
    file = session.get(MaterialFile, UUID(version["ordered_material_ids"][0]))
    file.storage_state = "unavailable"
    session.flush()
    with pytest.raises(DomainError) as error:
        presets.load_preset_into_draft(
            session,
            scope,
            version["id"],
            uuid4(),
            draft.revision,
            drama_id=drama.drama_id,
            allow_reuse=True,
        )
    assert error.value.code == "image_candidate_unavailable"


def test_public_api_scope_strict_bodies_and_copy(
    client, session, context, image_draft, other_context
):
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User

    draft, drama, scope = image_draft
    source = apply_rule(session, image_draft)[0]
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, context.actor_id
    )
    url = f"/api/tenants/{context.tenant_id}/materials/carousel-presets"
    body = {
        "draft_id": str(draft.id),
        "expected_revision": draft.revision,
        "request_id": str(uuid4()),
        "source_unit_id": str(source["id"]),
        "name": "HTTP version",
    }
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    version = response.json()
    assert client.post(url, json=body).json() == version
    assert client.get(url + "?name=HTTP&limit=1").json()["total"] == 1
    assert client.get(url + f"/{version['id']}").status_code == 200
    copy_body = {"name": body["name"]}
    copy_body["request_id"] = str(uuid4())
    assert (
        client.post(url + f"/{version['id']}/copy", json=copy_body).json()["version"]
        == 2
    )
    load_body = {
        "draft_id": str(draft.id),
        "expected_revision": draft.revision,
        "request_id": str(uuid4()),
        "drama_id": str(drama.drama_id),
        "allow_reuse": True,
    }
    response = client.post(url + f"/{version['id']}/load", json=load_body)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == body["expected_revision"] + 1
    assert (
        client.post(
            url + f"/{version['id']}/load", json={**load_body, "route": {}}
        ).status_code
        == 422
    )
    assert (
        client.get(
            url.replace(str(context.tenant_id), str(other_context.tenant_id))
        ).status_code
        == 403
    )


def test_preset_references_are_typed_scoped_complete_and_block_deletion(
    session, image_draft, other_context
):
    from app.modules.materials.carousel_models import (
        CarouselPresetItem,
        CarouselPresetVersion,
    )
    from tests.modules.materials.test_tenant_materials import material

    apply_rule(session, image_draft)
    source = save(session, image_draft)
    scope = image_draft[2]
    isolated = material(session, scope.context, "library-only.jpg")
    isolated.media_type, isolated.sha256 = "IMAGE", "a" * 64
    session.flush()
    row = CarouselPresetVersion(
        **{
            **source,
            "id": uuid4(),
            "version": 8,
            "actor_id": scope.actor_id,
            "ordered_material_ids": [str(isolated.id)],
        }
    )
    session.add(row)
    session.flush()
    session.add(
        CarouselPresetItem(
            tenant_id=scope.tenant_id,
            version_id=row.id,
            position=1,
            material_id=isolated.id,
            content_digest=isolated.sha256,
        )
    )
    session.flush()
    with pytest.raises(IntegrityError) as error, session.begin_nested():
        session.delete(isolated)
        session.flush()
    assert error.value.orig.diag.constraint_name == "fk_carousel_preset_item_material"
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            CarouselPresetItem(
                tenant_id=scope.tenant_id,
                version_id=row.id,
                position=2,
                material_id=isolated.id,
                content_digest=isolated.sha256,
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            CarouselPresetItem(
                tenant_id=other_context.tenant_id,
                version_id=row.id,
                position=1,
                material_id=isolated.id,
                content_digest=isolated.sha256,
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        bad = CarouselPresetVersion(
            **{
                **source,
                "id": uuid4(),
                "version": 9,
                "actor_id": scope.actor_id,
                "ordered_material_ids": [str(isolated.id)],
            }
        )
        session.add(bad)
        session.flush()
        session.add(
            CarouselPresetItem(
                tenant_id=scope.tenant_id,
                version_id=bad.id,
                position=1,
                material_id=isolated.id,
                media_type="VIDEO",
                content_digest=isolated.sha256,
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        incomplete = CarouselPresetVersion(
            **{**source, "id": uuid4(), "version": 10, "actor_id": scope.actor_id}
        )
        session.add(incomplete)
        session.flush()
        session.execute(text("SET CONSTRAINTS carousel_preset_complete IMMEDIATE"))


def test_available_original_retained_and_cleaned_original_not_resurrected(
    session, image_draft
):
    from datetime import UTC, datetime, timedelta

    from app.modules.materials.cleanup import _active_uses
    from app.modules.materials.ingest_models import OriginalUse
    from app.modules.materials.models import MaterialFile
    from app.modules.materials.object_uses import (
        release_original_use,
        retain_preset_original,
    )
    from tests.modules.materials.test_ingest_models import original

    source = apply_rule(session, image_draft)[0]
    scope = image_draft[2]
    file = session.get(MaterialFile, source["ordered_material_ids"][0])
    obj = original(
        session,
        scope.context,
        file,
        status="verified",
        actual_bytes=file.byte_size,
        sha256=file.sha256,
        video_md5="b" * 32,
        digest_verified_at=datetime.now(UTC),
    )
    file.current_object_generation = obj.generation
    session.flush()
    version = save(session, image_draft)
    use = session.exec(
        select(OriginalUse).where(OriginalUse.operation_id == UUID(version["id"]))
    ).one()
    assert use.purpose == "carousel_preset"
    assert (
        retain_preset_original(
            session,
            context=scope.context,
            material_id=file.id,
            version_id=UUID(version["id"]),
        ).id
        == use.id
    )
    use.expires_at = datetime.now(UTC) - timedelta(days=100)
    assert use in _active_uses(session, obj)
    assert not release_original_use(session, use_id=use.id, nonce=use.nonce)
    assert obj.status == "verified"
    # 历史清理事实不可逆：另一个成员原件已删除时另存版本仍有记录 FK，但不伪造原件。
    cleaned_file = session.get(MaterialFile, source["ordered_material_ids"][1])
    cleaned = original(session, scope.context, cleaned_file, status="deleted")
    cleaned_file.current_object_generation = cleaned.generation
    session.flush()
    second = save(session, image_draft)
    assert cleaned.status == "deleted"
    assert not session.exec(
        select(OriginalUse).where(OriginalUse.material_id == cleaned_file.id)
    ).all()
    assert second["version"] == 2


@pytest.fixture
def committed_preset_case(monkeypatch):
    from sqlmodel import Session

    from tests.migration_database import historical_database
    from tests.modules.conftest import create_context

    # 专用一次性数据库承载真实多会话，不污染/广泛清理主测试数据库。
    with historical_database(monkeypatch, "head") as (engine, _):
        with Session(engine) as session:
            context = create_context(session)
            case = image_draft.__wrapped__(session, context)
            apply_rule(session, case)
            version = save(session, case)
            draft, drama, scope = case
            revision, drama_id = draft.revision, drama.drama_id
            session.commit()
        yield engine, scope, revision, drama_id, version


def test_concurrent_load_replay_and_stale_revision(committed_preset_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.builds.models import BuildDraft

    engine, scope, revision, drama_id, version = committed_preset_case
    request = uuid4()
    barrier = Barrier(2)

    def load(request_id, expected):
        with Session(engine) as session:
            barrier.wait()
            try:
                result = presets.load_preset_into_draft(
                    session,
                    scope,
                    version["id"],
                    request_id,
                    expected,
                    drama_id=drama_id,
                    allow_reuse=True,
                )
                session.commit()
                return result
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: load(request, revision), range(2)))
    assert a == b and a["revision"] == revision + 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: load(uuid4(), revision + 1), range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "draft_revision_conflict" in results
    with Session(engine) as session:
        assert session.get(BuildDraft, scope.draft_id).revision == revision + 2


def test_concurrent_same_name_saves_allocate_distinct_immutable_versions(
    committed_preset_case,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    engine, scope, revision, _, version = committed_preset_case
    barrier = Barrier(2)

    def copy(_):
        with Session(engine) as session:
            barrier.wait()
            result = presets.copy_preset_version(
                session, scope.context, version["id"], version["name"], uuid4()
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(copy, range(2)))
    assert {r["version"] for r in results} == {2, 3}
    with Session(engine) as session:
        assert (
            presets.get_preset_version(session, scope.context, version["id"]) == version
        )


def test_0054_upgrade_preserves_units_and_installs_reference_constraints(monkeypatch):
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect
    from sqlmodel import Session

    from tests.migration_database import historical_database
    from tests.modules.builds.test_image_composition_migration import (
        historical_scaffold,
    )
    from tests.modules.conftest import create_context

    with historical_database(monkeypatch, "0054_music_preparation") as (engine, config):
        with Session(engine) as session:
            context = create_context(session)
            from tests.modules.builds.test_drafts import create_intent
            from tests.modules.builds.test_image_composition_migration import unit

            draft, drama = historical_scaffold(
                session,
                context,
                create_intent(session, context, historical=True),
                active_image_generation=1,
            )
            source = unit(draft, drama)
            session.add(source)
            session.flush()
            source_id, draft_id, drama_id = source.id, draft.id, drama.drama_id
            session.commit()
        command.upgrade(config, "head")
        with Session(engine) as session:
            from app.modules.builds.image_composition_models import DraftCreativeUnit

            restored = session.get(
                DraftCreativeUnit, (context.tenant_id, draft_id, 1, source_id)
            )
            assert restored is not None and restored.drama_id == drama_id
            keys = inspect(session.connection()).get_foreign_keys(
                "carousel_preset_item"
            )
            assert {k["name"] for k in keys} == {
                "fk_carousel_preset_item_version",
                "fk_carousel_preset_item_material",
            }
            assert (
                session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == ScriptDirectory.from_config(config).get_current_head()
            )


def uploaded_original(session, scope, material_id):
    """真实持久化的 IMAGE 上传/读回回执；无 provider 或存储调用。"""
    from datetime import UTC, datetime

    from app.modules.materials.models import (
        AccountMaterial,
        MaterialAssetOperation,
        MaterialFile,
        MaterialUploadAttempt,
    )
    from tests.modules.materials.test_ingest_models import original

    file = session.get(MaterialFile, material_id)
    from app.modules.materials.ingest_models import IngestSession, IngestSessionFile

    batch = IngestSession(
        tenant_id=scope.tenant_id,
        bc_id=file.bc_id,
        actor_id=scope.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        expected_files=1,
        expected_bytes=file.byte_size,
    )
    session.add(batch)
    session.flush()
    session.add(
        IngestSessionFile(
            tenant_id=scope.tenant_id,
            bc_id=file.bc_id,
            session_id=batch.id,
            client_index=0,
            media_type="IMAGE",
            material_id=file.id,
            byte_size=file.byte_size,
            manifest_digest="a" * 64,
        )
    )
    session.flush()
    now = datetime.now(UTC)
    obj = original(
        session,
        scope.context,
        file,
        status="verified",
        actual_bytes=file.byte_size,
        sha256=file.sha256,
        video_md5="b" * 32,
        digest_verified_at=now,
        storage_provider="r2",
        storage_endpoint="https://test.r2.cloudflarestorage.com",
        storage_bucket="test-private",
    )
    file.current_object_generation, file.video_md5, file.digest_verified_at = (
        obj.generation,
        obj.video_md5,
        now,
    )
    from app.modules.accounts.models import BCAccountAccess

    session.add(
        BCAccountAccess(
            tenant_id=scope.tenant_id,
            bc_id=file.bc_id,
            advertiser_id="account-A",
            connection_id=scope.route.connection_id,
            in_bc=True,
            authorized=True,
            active=True,
        )
    )
    session.flush()
    asset = AccountMaterial(
        tenant_id=scope.tenant_id,
        bc_id=file.bc_id,
        material_id=file.id,
        advertiser_id="account-A",
        connection_id=scope.route.connection_id,
        media_type="IMAGE",
        image_id="image-retention",
        is_carousel_usable=True,
        status="available",
        verified_at=now,
    )
    session.add(asset)
    session.flush()
    operation = MaterialAssetOperation(
        tenant_id=scope.tenant_id,
        bc_id=file.bc_id,
        material_id=file.id,
        advertiser_id=asset.advertiser_id,
        path="upload_original",
        status="succeeded",
        request_digest="a" * 64,
        remote_response={
            "object_id": str(obj.id),
            "generation": obj.generation,
            "image_id": asset.image_id,
            "confirmation_source": "image_readback",
        },
    )
    session.add(operation)
    session.flush()
    session.add(
        MaterialUploadAttempt(
            tenant_id=scope.tenant_id,
            bc_id=file.bc_id,
            material_id=file.id,
            advertiser_id=asset.advertiser_id,
            connection_id=asset.connection_id,
            operation_id=operation.id,
            request_digest="a" * 64,
            status="available",
        )
    )
    session.flush()
    return obj, operation


def test_preset_retention_prevents_cleanup_scheduling(session, image_draft):
    from app.modules.materials.cleanup import schedule_cleanup
    from app.modules.materials.ingest_models import ObjectCleanup

    source = apply_rule(session, image_draft)[0]
    obj, operation = uploaded_original(
        session, image_draft[2], source["ordered_material_ids"][0]
    )
    save(session, image_draft)
    assert (
        schedule_cleanup(session, object_id=obj.id, source_receipt_id=operation.id)
        is None
    )
    assert obj.status == "verified"
    assert not session.exec(
        select(ObjectCleanup).where(ObjectCleanup.material_id == obj.material_id)
    ).all()


def test_save_vs_cleanup_serialize_on_original_lock(committed_preset_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.materials.cleanup import schedule_cleanup
    from app.modules.materials.ingest_models import OriginalUse, TemporaryMaterialObject

    engine, scope, revision, _, version = committed_preset_case
    with Session(engine) as session:
        obj, operation = uploaded_original(
            session, scope, UUID(version["ordered_material_ids"][0])
        )
        object_id, operation_id, material_id = obj.id, operation.id, obj.material_id
        session.commit()
    barrier = Barrier(2)

    def write(kind):
        with Session(engine) as session:
            barrier.wait()
            if kind == "save":
                result = presets.save_preset_version(
                    session, scope, version["source_unit_id"], revision, "race", uuid4()
                )
            else:
                result = schedule_cleanup(
                    session, object_id=object_id, source_receipt_id=operation_id
                )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        saved, cleanup_id = list(pool.map(write, ["save", "cleanup"]))
    with Session(engine) as session:
        obj = session.get(TemporaryMaterialObject, object_id)
        references = session.exec(
            select(OriginalUse).where(
                OriginalUse.material_id == material_id,
                OriginalUse.purpose == "carousel_preset",
            )
        ).all()
        if cleanup_id is None:
            assert obj.status == "verified" and len(references) == 1
        else:
            assert obj.status == "cleanup_pending" and references == []
        assert (
            presets.get_preset_version(session, scope.context, saved["id"])[
                "ordered_material_ids"
            ]
            == version["ordered_material_ids"]
        )


def test_library_copy_no_draft_replay_conflict_and_permission(
    client, session, context, image_draft
):
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.tenants.models import TenantMembership

    apply_rule(session, image_draft)
    version = save(session, image_draft)
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, context.actor_id
    )
    # 撤销原 BC 执行绑定仍能复制租户库版本，不会伪造新来源或执行路线。
    binding = session.get(
        BCConnectionBinding,
        (context.tenant_id, image_draft[2].bc_id, image_draft[2].route.connection_id),
    )
    binding.status = "DISABLED"
    session.flush()
    url = f"/api/tenants/{context.tenant_id}/materials/carousel-presets/{version['id']}/copy"
    body = {"name": "No draft", "request_id": str(uuid4())}
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    copied = response.json()
    assert copied["source_bc_id"] == version["source_bc_id"]
    assert client.post(url, json=body).json() == copied
    assert client.post(url, json={**body, "name": "changed"}).status_code == 409
    assert (
        client.post(url.replace(version["id"], copied["id"]), json=body).status_code
        == 409
    )
    assert client.post(url, json={**body, "draft_id": str(uuid4())}).status_code == 422
    session.get(TenantMembership, (context.tenant_id, context.actor_id)).role = "viewer"
    session.flush()
    assert client.post(url, json=body).status_code == 403


def test_concurrent_library_copy_replay_one_version(committed_preset_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    engine, scope, _, _, version = committed_preset_case
    request = uuid4()
    barrier = Barrier(2)

    def copy(_):
        with Session(engine) as session:
            barrier.wait()
            result = presets.copy_preset_version(
                session, scope.context, version["id"], "same request", request
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(copy, range(2)))
    assert a == b and a["version"] == 1
    with Session(engine) as session:
        assert (
            presets.list_preset_versions(session, scope.context, name="same request")[
                "total"
            ]
            == 1
        )


def test_copy_receipt_requires_both_identity_and_digest(session, image_draft):
    from app.modules.materials.carousel_models import CarouselPresetVersion

    apply_rule(session, image_draft)
    source = save(session, image_draft)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            CarouselPresetVersion(
                **{
                    **source,
                    "id": uuid4(),
                    "version": 99,
                    "actor_id": image_draft[2].actor_id,
                    "copy_request_id": uuid4(),
                    "copy_request_digest": None,
                }
            )
        )
        session.flush()


def test_selected_song_metadata_and_single_needs_selection(session, image_draft):
    from app.modules.builds.image_composition_models import DraftCreativeUnit

    units = apply_rule(session, image_draft)
    draft, drama, scope = image_draft
    row = session.get(
        DraftCreativeUnit,
        (scope.tenant_id, draft.id, draft.active_image_generation, units[0]["id"]),
    )
    row.music_id, row.music_status = "chosen", "READY"
    row.music_snapshot = {
        "music_id": "chosen",
        "display_name": "Song",
        "author": "Artist",
        "duration": 0,
        "selection_source": "MANUAL",
        "preview_url": "https://expired",
        "pending_request": {"request_id": str(uuid4())},
        "job_id": str(uuid4()),
    }
    selected = save(session, image_draft)
    assert selected["music_snapshot"] == {
        "music_id": "chosen",
        "display_name": "Song",
        "author": "Artist",
        "duration": 0,
        "selection_source": "MANUAL",
    }
    single = save(session, image_draft, source=units[-1], name="Single")
    assert single["music_status"] == "NEEDS_SELECTION"
    loaded = presets.load_preset_into_draft(
        session,
        scope,
        single["id"],
        uuid4(),
        draft.revision,
        drama_id=drama.drama_id,
        allow_reuse=True,
    )
    assert loaded["unit"]["music_status"] == "NEEDS_SELECTION"
    assert loaded["unit"]["music_id"] is None and not loaded["unit"]["music_protected"]


def test_concurrent_different_name_copies_do_not_deadlock_on_material_fk(
    committed_preset_case,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local

    from sqlalchemy import event
    from sqlmodel import Session

    engine, scope, _, _, version = committed_preset_case
    barrier, seen = Barrier(2), local()

    def before_lock(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            "FROM material_file" in statement
            and "FOR UPDATE" in statement
            and not getattr(seen, "waited", False)
        ):
            seen.waited = True
            barrier.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", before_lock)

    def copy(index):
        with Session(engine) as session:
            result = presets.copy_preset_version(
                session, scope.context, version["id"], f"parallel-{index}", uuid4()
            )
            session.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(copy, range(2)))
    finally:
        event.remove(engine, "before_cursor_execute", before_lock)
    assert len({r["id"] for r in results}) == 2
