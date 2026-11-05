"""真实 PostgreSQL 的候选、CAS 与原子 generation 验证。"""

from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.image_composition_commands import (
    Create,
    Delete,
    RestoreCandidates,
)
from app.modules.builds.image_composition_schemas import ImageRule
from app.modules.builds.image_composition_service import (
    adopt_candidates,
    advance_composition,
    apply_composition,
    edit_creative_units,
    list_creative_units,
    preview_composition,
    refresh_candidates,
    resolve_image_scope,
)
from app.modules.builds.models import BuildDraft
from tests.modules.builds.test_drafts import create_intent
from tests.modules.builds.test_image_composition_migration import scaffold
from tests.modules.materials.test_tenant_materials import material


def finish_candidate_snapshot(session, scope, progress):
    from app.modules.builds.image_candidate_service import (
        advance_candidate_snapshot,
        candidate_snapshot_progress,
    )

    for _ in range(100):
        progress = candidate_snapshot_progress(session, scope, progress["snapshot_id"])
        if progress["status"] != "PENDING":
            assert progress["status"] == "READY"
            return progress
        advance_candidate_snapshot(
            session, progress["snapshot_id"], progress["generation"]
        )
    raise AssertionError("candidate construction did not finish")


@pytest.fixture
def image_draft(session, context):
    intent = create_intent(session, context)
    intent["drama_lines"] = ["Moon"]
    draft, drama = scaffold(session, context, intent)
    for index in range(7):
        row = material(session, context, f"{drama.title}_{index}.jpg")
        row.media_type, row.mime_type, row.sha256 = (
            "IMAGE",
            "image/jpeg",
            f"{index:064x}",
        )
    session.flush()
    scope = resolve_image_scope(session, context=context, draft_id=draft.id)
    snapshot = refresh_candidates(session, scope, uuid4(), draft.revision)
    snapshot = finish_candidate_snapshot(session, scope, snapshot)
    adopt_candidates(session, scope, uuid4(), draft.revision, snapshot["snapshot_id"])
    return draft, drama, scope


def test_preview_replay_atomic_apply_and_exclusions(session, image_draft):
    draft, drama, scope = image_draft
    request = uuid4()
    plan = preview_composition(
        session, scope, request, draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert plan == preview_composition(
        session,
        scope,
        request,
        plan["base_revision"] - 1,
        ImageRule(),
        {},
        "PRESERVE_MANUAL",
    )
    with pytest.raises(DomainError, match="请求"):
        preview_composition(
            session,
            scope,
            request,
            draft.revision,
            ImageRule(images_per_unit=2),
            {},
            "PRESERVE_MANUAL",
        )
    assert list_creative_units(session, scope).total == 0
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    progress = advance_composition(session, run["run_id"], run["generation"], limit=1)
    assert progress["processed"] == 1
    assert draft.active_image_generation == 0
    while progress["status"] == "PENDING":
        progress = advance_composition(
            session, run["run_id"], run["generation"], limit=1
        )
    assert list_creative_units(session, scope).total == 3
    first = list_creative_units(session, scope).items[0]
    edit_creative_units(
        session,
        scope,
        uuid4(),
        draft.revision,
        Delete(kind="DELETE", unit_id=first["id"]),
    )
    next_plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert next_plan["unit_count"] == 2


def apply_rule(session, case, rule=None, mode="PRESERVE_MANUAL"):
    draft, drama, scope = case
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, rule or ImageRule(), {}, mode
    )
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    progress = advance_composition(session, run["run_id"], run["generation"])
    while progress["status"] == "PENDING":
        progress = advance_composition(session, run["run_id"], run["generation"])
    assert progress["status"] == "APPLIED"
    return list_creative_units(session, scope).items


def edit(session, case, command):
    draft, _, scope = case
    return edit_creative_units(session, scope, uuid4(), draft.revision, command)


def test_all_manual_commands_and_explicit_restore(session, image_draft):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import (
        Duplicate,
        Merge,
        SetImages,
        SetMusic,
        SetProtection,
        Split,
    )
    from app.modules.builds.image_composition_service import list_candidates

    draft, drama, scope = image_draft
    first, second, third = apply_rule(session, image_draft)
    one, two = UUID(first["id"]), UUID(second["id"])
    ids = tuple(map(UUID, first["ordered_material_ids"]))
    music = edit(
        session, image_draft, SetMusic(kind="SET_MUSIC", unit_id=one, music_id="manual")
    )
    assert not music["pending_music_ids"]
    edit(
        session,
        image_draft,
        SetImages(kind="SET_IMAGES", unit_id=one, material_ids=tuple(reversed(ids))),
    )
    current = list_creative_units(session, scope).items[0]
    assert current["music_id"] == "manual" and current["composition_protected"]
    assert current["music_snapshot"]["images_changed"]
    result = edit(session, image_draft, Duplicate(kind="DUPLICATE", unit_id=one))
    assert result["affected_units"][0] != str(one)
    clone = list_creative_units(session, scope).items[-1]
    assert clone["music_id"] == "manual" and clone["music_protected"]
    with pytest.raises(DomainError) as protected:
        edit(
            session, image_draft, Split(kind="SPLIT", unit_id=one, split_positions=(1,))
        )
    assert protected.value.code == "image_protected_music_conflict"
    edit(
        session,
        image_draft,
        SetProtection(kind="SET_PROTECTION", unit_id=one, music_protected=False),
    )
    edit(session, image_draft, Split(kind="SPLIT", unit_id=one, split_positions=(1,)))
    current = list_creative_units(session, scope).items
    assert all(u["music_id"] is None for u in current[:2])
    edit(
        session,
        image_draft,
        Merge(kind="MERGE", unit_ids=tuple(UUID(u["id"]) for u in current[:2])),
    )
    assert list_creative_units(session, scope).items[0]["ordered_material_ids"] == list(
        map(str, reversed(ids))
    )
    with pytest.raises(DomainError) as reuse:
        edit(
            session,
            image_draft,
            Create(kind="CREATE", drama_id=drama.drama_id, material_ids=ids),
        )
    assert reuse.value.code == "image_reuse_confirmation_required"
    edit(
        session,
        image_draft,
        Create(
            kind="CREATE", drama_id=drama.drama_id, material_ids=ids, allow_reuse=True
        ),
    )
    edit(session, image_draft, Delete(kind="DELETE", unit_id=two))
    excluded = [u for u in list_candidates(session, scope).items if not u["enabled"]]
    assert len(excluded) == 3
    with pytest.raises(DomainError) as excluded_error:
        edit(
            session,
            image_draft,
            Create(
                kind="CREATE",
                drama_id=drama.drama_id,
                material_ids=(UUID(excluded[0]["material_id"]),),
            ),
        )
    assert excluded_error.value.code == "image_candidate_unavailable"
    edit(
        session,
        image_draft,
        RestoreCandidates(
            kind="RESTORE_CANDIDATES",
            drama_id=drama.drama_id,
            material_ids=(UUID(excluded[0]["material_id"]),),
        ),
    )
    edit(
        session,
        image_draft,
        Create(
            kind="CREATE",
            drama_id=drama.drama_id,
            material_ids=(UUID(excluded[0]["material_id"]),),
        ),
    )
    assert (
        len([u for u in list_candidates(session, scope).items if not u["enabled"]]) == 2
    )


def test_protected_composition_music_conflict_and_batch_track_change(
    session, image_draft
):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import SetMusic, SetProtection

    draft, _, scope = image_draft
    rows = apply_rule(
        session, image_draft, ImageRule(music_mode="FIXED", fixed_music_id="A")
    )
    first = UUID(rows[0]["id"])
    edit(
        session,
        image_draft,
        SetMusic(kind="SET_MUSIC", unit_id=first, music_id="manual"),
    )
    plan = preview_composition(
        session,
        scope,
        uuid4(),
        draft.revision,
        ImageRule(images_per_unit=2),
        {},
        "PRESERVE_MANUAL",
    )
    assert any(
        c["code"] == "PROTECTED_MUSIC_COMPOSITION_CHANGED" for c in plan["conflicts"]
    )
    with pytest.raises(DomainError):
        apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    edit(
        session,
        image_draft,
        SetProtection(kind="SET_PROTECTION", unit_id=first, composition_protected=True),
    )
    result = apply_rule(
        session, image_draft, ImageRule(music_mode="FIXED", fixed_music_id="B")
    )
    assert result[0]["music_id"] == "manual"
    assert all(u["music_id"] == "B" for u in result[1:])
    result = apply_rule(
        session,
        image_draft,
        ImageRule(music_mode="FIXED", fixed_music_id="B"),
        "REPLACE_ALL",
    )
    assert all(u["music_id"] == "B" and not u["composition_protected"] for u in result)


def test_trial_music_completion_inherited_but_manual_selection_stales_plan(
    session, image_draft
):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import SetMusic
    from app.modules.builds.image_composition_models import DraftCreativeUnit

    draft, _, scope = image_draft
    first = apply_rule(session, image_draft)[0]
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    revision = draft.revision
    unit = session.get(
        DraftCreativeUnit,
        (scope.tenant_id, draft.id, draft.active_image_generation, UUID(first["id"])),
    )
    unit.music_id, unit.music_status, unit.music_revision = (
        "completed",
        "READY",
        unit.music_revision + 1,
    )
    unit.music_snapshot = {"name": "song"}
    session.flush()
    assert draft.revision == revision
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), revision)
    assert (
        advance_composition(session, run["run_id"], run["generation"])["status"]
        == "APPLIED"
    )
    assert list_creative_units(session, scope).items[0]["music_id"] == "completed"
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    edit(
        session,
        image_draft,
        SetMusic(kind="SET_MUSIC", unit_id=UUID(first["id"]), music_id="manual"),
    )
    with pytest.raises(DomainError) as stale:
        apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    assert stale.value.code == "draft_revision_conflict"


def test_music_completed_between_staging_chunks_is_inherited_at_switch(
    session, image_draft
):
    from uuid import UUID

    from app.modules.builds.image_composition_models import DraftCreativeUnit

    draft, _, scope = image_draft
    first = apply_rule(session, image_draft)[0]
    old_generation = draft.active_image_generation
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    advance_composition(session, run["run_id"], run["generation"], limit=1)
    unit = session.get(
        DraftCreativeUnit,
        (scope.tenant_id, draft.id, old_generation, UUID(first["id"])),
    )
    unit.music_id, unit.music_status, unit.music_revision = (
        "late-completed",
        "READY",
        unit.music_revision + 1,
    )
    assert (
        advance_composition(session, run["run_id"], run["generation"])["status"]
        == "APPLIED"
    )
    assert list_creative_units(session, scope).items[0]["music_id"] == "late-completed"


def test_refresh_candidate_acceptance_stales_plan_and_preserves_active_and_exclusions(
    session, image_draft
):
    from uuid import UUID

    from app.modules.builds.image_composition_service import list_candidates

    draft, drama, scope = image_draft
    units = apply_rule(session, image_draft)
    edit(session, image_draft, Delete(kind="DELETE", unit_id=UUID(units[0]["id"])))
    old = list_creative_units(session, scope).items
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    extra = material(session, scope.context, "Moon_late.jpg")
    extra.media_type, extra.sha256 = "IMAGE", "f" * 64
    session.flush()
    snapshot = refresh_candidates(session, scope, uuid4(), draft.revision)
    assert snapshot["added"] == 1
    assert list_candidates(session, scope).total == 7
    snapshot = finish_candidate_snapshot(session, scope, snapshot)
    adopt_candidates(session, scope, uuid4(), draft.revision, snapshot["snapshot_id"])
    assert list_candidates(session, scope).total == 8
    assert (
        len([r for r in list_candidates(session, scope).items if not r["enabled"]]) == 3
    )
    assert list_creative_units(session, scope).items == old
    with pytest.raises(DomainError) as stale:
        apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    assert stale.value.code == "draft_revision_conflict"


def test_failure_cancel_and_old_worker_replay_preserve_active(session, image_draft):
    from uuid import UUID

    from app.modules.builds.image_composition_service import cancel_composition
    from app.modules.materials.models import MaterialFile

    draft, _, scope = image_draft
    old = apply_rule(session, image_draft)
    active = draft.active_image_generation
    plan = preview_composition(
        session,
        scope,
        uuid4(),
        draft.revision,
        ImageRule(images_per_unit=2),
        {},
        "PRESERVE_MANUAL",
    )
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    advance_composition(session, run["run_id"], run["generation"], limit=1)
    cancel_composition(session, scope, run["run_id"], uuid4(), draft.revision)
    assert (
        advance_composition(session, run["run_id"], run["generation"])["status"]
        == "CANCELLED"
    )
    assert draft.active_image_generation == active
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    advance_composition(session, run["run_id"], run["generation"], limit=1)
    broken = session.get(MaterialFile, UUID(old[0]["ordered_material_ids"][0]))
    broken.storage_state = "unavailable"
    progress = advance_composition(session, run["run_id"], run["generation"])
    assert (
        progress["status"] == "FAILED"
        and progress["error_code"] == "image_candidate_unavailable"
    )
    assert draft.active_image_generation == active
    assert list_creative_units(session, scope).items == old


def test_edits_reject_cross_tenant_drama_stale_revision_and_changed_replay(
    session, image_draft, other_context
):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import SetImages

    draft, drama, scope = image_draft
    first = apply_rule(session, image_draft)[0]
    foreign = material(session, other_context, "Moon_foreign.jpg")
    foreign.media_type, foreign.sha256 = "IMAGE", "f" * 64
    session.flush()
    for ids in ((foreign.id,), (UUID(first["ordered_material_ids"][0]),) * 2):
        with pytest.raises(DomainError):
            edit(
                session,
                image_draft,
                SetImages(
                    kind="SET_IMAGES", unit_id=UUID(first["id"]), material_ids=ids
                ),
            )
    revision, request = draft.revision, uuid4()
    command = Delete(kind="DELETE", unit_id=UUID(first["id"]))
    result = edit_creative_units(session, scope, request, revision, command)
    assert result == edit_creative_units(session, scope, request, revision, command)
    with pytest.raises(DomainError) as replay:
        edit_creative_units(session, scope, request, revision + 1, command)
    assert replay.value.code == "idempotency_conflict"
    with pytest.raises(DomainError) as stale:
        edit_creative_units(session, scope, uuid4(), revision, command)
    assert stale.value.code == "draft_revision_conflict"


def test_pagination_binds_scope_snapshot_generation_and_revision(session, image_draft):
    from app.modules.builds.image_composition_service import (
        list_candidates,
        list_plan_units,
    )

    draft, _, scope = image_draft
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    page = list_plan_units(session, scope, plan["plan_id"], limit=1)
    second = list_plan_units(
        session, scope, plan["plan_id"], cursor=page.next_cursor, limit=1
    )
    assert page.total == 3 and second.items[0]["id"] != page.items[0]["id"]
    with pytest.raises(DomainError):
        list_candidates(session, scope, cursor=page.next_cursor)
    apply_rule(session, image_draft)
    page = list_creative_units(session, scope, limit=1)
    apply_rule(session, image_draft)
    with pytest.raises(DomainError):
        list_creative_units(session, scope, cursor=page.next_cursor)


def test_new_carousel_draft_prepares_image_scene_after_matching_candidates(
    session, context, monkeypatch
):
    from app.modules.builds import drafts
    from app.modules.builds.image_composition_service import list_candidates
    from app.modules.builds.models import DraftGroupMaterial
    from tests.modules.builds.test_drafts import account, finish, ready_links

    intent = create_intent(session, context)
    intent["drama_lines"] = ["Moon"]
    account(session, context)
    image = material(session, context, "Moon_1.jpg")
    image.media_type, image.sha256 = "IMAGE", "e" * 64
    material(session, context, "Moon_video.mp4")
    identity = drafts.create_draft(
        session, context=context, media_kind="CAROUSEL", **intent
    )
    task = drafts.prepare_draft(
        session, context=context, draft_id=identity, request_id=uuid4()
    )
    ready_links(session, context, task, intent)

    from app.modules.builds.scene_schemas import ScenePreparation

    scenes = []

    def image_scene(_session, **kwargs):
        scenes.append(kwargs)
        return ScenePreparation(None, "blocked", "fixture_scene_unavailable")

    monkeypatch.setattr(drafts, "ensure_scene_preparation", image_scene)
    finish(session, context, task)
    scope = resolve_image_scope(session, context=context, draft_id=identity)
    assert list_candidates(session, scope).total == 1
    assert len(scenes) == 1 and scenes[0]["media_kind"] == "CAROUSEL"
    assert not session.exec(
        select(DraftGroupMaterial).where(DraftGroupMaterial.draft_id == identity)
    ).all()
    draft = session.get(drafts.BuildDraft, identity)
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert plan["unit_count"] == 1 and not plan["conflicts"]


def test_carousel_selection_rebuilds_scene_without_replacing_composition(
    session, context, image_draft, monkeypatch
):
    from app.modules.builds import drafts
    from app.modules.builds.models import DraftPreparation
    from app.modules.builds.scene_schemas import ScenePreparation

    draft, _, scope = image_draft
    units = apply_rule(session, image_draft)
    generation = draft.active_image_generation
    scenes = []

    def image_scene(_session, **kwargs):
        scenes.append(kwargs)
        return ScenePreparation(None, "blocked", "fixture_scene_unavailable")

    monkeypatch.setattr(drafts, "ensure_scene_preparation", image_scene)
    draft.status = "DRAFT"
    task = drafts.prepare_selected_mini(
        session, context=context, draft_id=draft.id, request_id=uuid4()
    )
    assert session.get(DraftPreparation, task).phase == "scenes"
    drafts.continue_draft(session, context=context, task_id=task)
    assert len(scenes) == 1 and scenes[0]["media_kind"] == "CAROUSEL"
    assert draft.active_image_generation == generation
    assert [u["id"] for u in list_creative_units(session, scope).items] == [
        u["id"] for u in units
    ]


def test_cross_drama_allocation_and_explicit_reuse_are_resolvable(session, context):
    from app.modules.builds.image_composition_service import (
        list_candidates,
        resolve_candidate_conflict,
    )

    intent = create_intent(session, context)
    draft, drama = scaffold(session, context, intent)
    shared = material(session, context, "Moon Short Drama shared.jpg")
    shared.media_type, shared.sha256 = "IMAGE", "d" * 64
    for index, name in enumerate(("Moon.jpg", "Short Drama.jpg")):
        row = material(session, context, name)
        row.media_type, row.sha256 = "IMAGE", str(index) * 64
    session.flush()
    scope = resolve_image_scope(session, context=context, draft_id=draft.id)
    snapshot = refresh_candidates(session, scope, uuid4(), draft.revision)
    snapshot = finish_candidate_snapshot(session, scope, snapshot)
    adopt_candidates(session, scope, uuid4(), draft.revision, snapshot["snapshot_id"])
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    conflict = next(c for c in plan["conflicts"] if c["code"] == "CROSS_DRAMA_IMAGE")
    assert len(conflict["drama_ids"]) == 2
    resolve_candidate_conflict(
        session, scope, uuid4(), draft.revision, shared.id, conflict["drama_ids"]
    )
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert not plan["conflicts"]
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    assert (
        advance_composition(session, run["run_id"], run["generation"])["status"]
        == "APPLIED"
    )
    resolve_candidate_conflict(
        session, scope, uuid4(), draft.revision, shared.id, conflict["drama_ids"][:1]
    )
    assert (
        len([i for i in list_candidates(session, scope).items if not i["enabled"]]) == 1
    )


def test_http_api_scopes_and_strict_commands(client, session, context, image_draft):
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User

    draft, _, scope = image_draft
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, context.actor_id
    )
    url = f"/api/tenants/{context.tenant_id}/build-drafts/{draft.id}/image-composition"
    response = client.post(
        url + "/plans",
        json={
            "request_id": str(uuid4()),
            "expected_revision": draft.revision,
            "rule": {},
        },
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    response = client.post(
        url + "/apply",
        json={
            "request_id": str(uuid4()),
            "expected_revision": plan["base_revision"],
            "plan_id": plan["plan_id"],
        },
    )
    assert response.status_code == 200, response.text
    run = response.json()
    assert client.get(url + f"/runs/{run['run_id']}").json()["status"] == "PENDING"
    assert client.get(url + f"/plans/{plan['plan_id']}/units?limit=1").json()[
        "next_cursor"
    ]
    assert (
        client.post(
            url + "/edits",
            json={
                "request_id": str(uuid4()),
                "expected_revision": draft.revision,
                "command": {
                    "kind": "DELETE",
                    "unit_id": str(uuid4()),
                    "tenant_id": str(uuid4()),
                },
            },
        ).status_code
        == 422
    )
    response = client.post(
        url + f"/runs/{run['run_id']}/cancel",
        json={"request_id": str(uuid4()), "expected_revision": draft.revision},
    )
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
    assert (
        client.get(url.replace(str(context.tenant_id), str(uuid4()))).status_code == 403
    )


@pytest.fixture
def committed_image_draft(monkeypatch):
    from sqlmodel import Session

    from tests.migration_database import historical_database
    from tests.modules.conftest import create_context

    # 不绕过策略不可变触发器清理；并发测试独占一次性数据库，完成后整库释放。
    with historical_database(monkeypatch, "head") as (database_engine, _):
        with Session(database_engine) as setup:
            context = create_context(setup)
            intent = create_intent(setup, context)
            intent["drama_lines"] = ["Moon"]
            draft, drama = scaffold(setup, context, intent)
            for index in range(61):
                row = material(setup, context, f"Moon_{index}.jpg")
                row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
            setup.flush()
            scope = resolve_image_scope(setup, context=context, draft_id=draft.id)
            snapshot = refresh_candidates(setup, scope, uuid4(), draft.revision)
            adopt_candidates(
                setup, scope, uuid4(), draft.revision, snapshot["snapshot_id"]
            )
            plan = preview_composition(
                setup,
                scope,
                uuid4(),
                draft.revision,
                ImageRule(),
                {},
                "PRESERVE_MANUAL",
            )
            setup.commit()
        yield scope, plan, database_engine


def test_actual_concurrent_apply_same_request_and_distinct_request(
    committed_image_draft,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.builds.image_composition_state import ImageCompositionRun

    scope, plan, engine = committed_image_draft
    request, gate = uuid4(), Barrier(2)

    def claim(request_id):
        with Session(engine) as worker:
            gate.wait(timeout=5)
            try:
                result = apply_composition(
                    worker, scope, plan["plan_id"], request_id, plan["base_revision"]
                )
                worker.commit()
                return result
            except DomainError as error:
                worker.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(claim, [request, request]))
    assert same[0] == same[1] and isinstance(same[0], dict)
    gate = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        distinct = list(pool.map(claim, [uuid4(), uuid4()]))
    assert distinct == ["image_composition_busy"] * 2
    with Session(engine) as verify:
        assert (
            len(
                verify.exec(
                    select(ImageCompositionRun).where(
                        ImageCompositionRun.draft_id == scope.draft_id
                    )
                ).all()
            )
            == 1
        )


def test_actual_concurrent_edit_vs_apply_has_exactly_one_winner(committed_image_draft):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.builds.image_composition_service import (
        exclude_candidates,
        list_candidates,
    )

    scope, plan, engine = committed_image_draft
    with Session(engine) as setup:
        row = list_candidates(setup, scope).items[0]
    from uuid import UUID

    gate = Barrier(2)

    def mutate(kind):
        with Session(engine) as worker:
            gate.wait(timeout=5)
            try:
                if kind == "apply":
                    apply_composition(
                        worker, scope, plan["plan_id"], uuid4(), plan["base_revision"]
                    )
                else:
                    exclude_candidates(
                        worker,
                        scope,
                        uuid4(),
                        plan["base_revision"],
                        UUID(row["drama_id"]),
                        (UUID(row["material_id"]),),
                    )
                worker.commit()
                return "success"
            except DomainError as error:
                worker.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(mutate, ["edit", "apply"]))
    assert outcomes.count("success") == 1
    assert set(outcomes) <= {
        "success",
        "image_composition_busy",
        "draft_revision_conflict",
    }


def test_worker_rollback_replay_recovery_and_real_redis_delivery(
    committed_image_draft, redis_client, redis_key_prefix
):
    import json
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from sqlmodel import Session

    from app.jobs.models import PendingDispatch
    from app.jobs.tasks import dispatch_queue
    from app.modules.builds.image_composition_state import ImageCompositionRun
    from app.modules.builds.image_composition_tasks import (
        TASK_NAME,
        process_composition,
        repair_compositions,
    )

    scope, plan, engine = committed_image_draft
    with Session(engine) as setup:
        run = apply_composition(
            setup, scope, plan["plan_id"], uuid4(), plan["base_revision"]
        )
        setup.commit()
    # 模拟 worker 在完成一片但尚未提交时退出，数据库回滚全部暂存和游标。
    with Session(engine) as crashed:
        progress = advance_composition(crashed, run["run_id"], run["generation"])
        assert progress["processed"] == 20 and progress["status"] == "PENDING"
        crashed.rollback()
    with Session(engine) as check:
        persisted = check.get(ImageCompositionRun, UUID(run["run_id"]))
        assert persisted.cursor == 0
        dispatch = check.get(PendingDispatch, persisted.dispatch_id)
        first_payload = dispatch.payload
        dispatch.published_at = datetime.now(UTC)
        persisted.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        check.commit()
    assert repair_compositions(database_engine=engine) == 1
    assert dispatch_queue(TASK_NAME) == "builds"
    # 真实 Redis DB14 上的私有队列仅传 outbox 的标识载荷；不触碰应用队列。
    queue = redis_key_prefix + ":image-composition"
    try:
        redis_client.rpush(queue, json.dumps(first_payload), json.dumps(first_payload))
        for _ in range(2):
            payload = json.loads(redis_client.lpop(queue))
            process_composition(
                database_engine=engine,
                tenant_id=scope.tenant_id,
                actor_id=scope.actor_id,
                payload=payload,
            )
        with Session(engine) as check:
            persisted = check.get(ImageCompositionRun, UUID(run["run_id"]))
            assert persisted.cursor == 20 and persisted.status == "PENDING"
            assert list_creative_units(check, scope).total == 0
            dispatch = check.get(PendingDispatch, persisted.dispatch_id)
            second_payload = dispatch.payload
        process_composition(
            database_engine=engine,
            tenant_id=scope.tenant_id,
            actor_id=scope.actor_id,
            payload=second_payload,
        )
        process_composition(
            database_engine=engine,
            tenant_id=scope.tenant_id,
            actor_id=scope.actor_id,
            payload=second_payload,
        )
        with Session(engine) as check:
            persisted = check.get(ImageCompositionRun, UUID(run["run_id"]))
            assert persisted.status == "APPLIED" and persisted.cursor == 21
            assert list_creative_units(check, scope).total == 21
            draft = check.get(BuildDraft, scope.draft_id)
            assert draft.revision == plan["base_revision"] + 1
    finally:
        redis_client.delete(queue)


def test_delete_duplicate_keeps_retained_protected_copy_usable(session, image_draft):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import Duplicate
    from app.modules.builds.image_composition_service import list_candidates

    draft, _, scope = image_draft
    original = apply_rule(session, image_draft)[0]
    duplicate = edit(
        session, image_draft, Duplicate(kind="DUPLICATE", unit_id=UUID(original["id"]))
    )
    edit(session, image_draft, Delete(kind="DELETE", unit_id=UUID(original["id"])))
    assert all(c["enabled"] for c in list_candidates(session, scope).items)
    result = apply_rule(session, image_draft)
    assert result[0]["id"] == duplicate["affected_units"][0]
    assert result[0]["composition_protected"]
    assert result[0]["ordered_material_ids"] == original["ordered_material_ids"]


def test_draft_input_reset_is_usable_and_preserves_history_account_change_keeps_units(
    session, image_draft
):
    from app.modules.builds.catalog import draft_summary
    from app.modules.builds.drafts import prepare_draft, update_draft
    from app.modules.builds.image_composition_service import list_candidates
    from app.modules.builds.image_composition_state import (
        ImageCandidateSnapshot,
        ImageCompositionState,
    )
    from tests.modules.builds.test_drafts import finish, ready_links

    draft, _, scope = image_draft
    old_units = apply_rule(session, image_draft)
    old_state = session.get(ImageCompositionState, (scope.tenant_id, draft.id))
    snapshot_id = old_state.snapshot_id
    old_generation = draft.active_image_generation
    update_draft(
        session,
        context=scope.context,
        draft_id=draft.id,
        expected_revision=draft.revision,
        account_lines=["account-A"],
    )
    assert draft.active_image_generation == old_generation
    assert list_creative_units(session, scope).items == old_units
    update_draft(
        session,
        context=scope.context,
        draft_id=draft.id,
        expected_revision=draft.revision,
        drama_lines=["", "New Story"],
    )
    assert draft.active_image_generation == 0
    assert session.get(ImageCandidateSnapshot, snapshot_id)
    assert list_candidates(session, scope).total == 0
    assert (
        draft_summary(session, context=scope.context, draft_id=draft.id).drama_count
        == 0
    )
    row = material(session, scope.context, "New Story_1.jpg")
    row.media_type, row.sha256 = "IMAGE", "e" * 64
    task = prepare_draft(
        session, context=scope.context, draft_id=draft.id, request_id=uuid4()
    )
    ready_links(
        session,
        scope.context,
        task,
        {
            "provider_connection_id": draft.provider_connection_id,
            "application_id": draft.application_id,
            "link_config": draft.link_config,
        },
    )
    finish(session, scope.context, task)
    assert list_candidates(session, scope).total == 1
    assert (
        draft_summary(session, context=scope.context, draft_id=draft.id).drama_count
        == 1
    )
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert plan["unit_count"] == 1 and len(plan["dramas"]) == 1


def test_revision_cas_fences_generic_account_edit_during_apply(session, image_draft):
    from app.modules.builds.drafts import update_draft

    draft, _, scope = image_draft
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    with pytest.raises(DomainError) as busy:
        update_draft(
            session,
            context=scope.context,
            draft_id=draft.id,
            expected_revision=draft.revision,
            account_lines=["account-A"],
        )
    assert busy.value.code == "image_composition_busy"


def test_revoked_binding_fails_generation_without_switch(session, image_draft):
    from app.modules.accounts.connection_models import BCConnectionBinding

    draft, _, scope = image_draft
    old = apply_rule(session, image_draft)
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    binding = session.get(
        BCConnectionBinding, (scope.tenant_id, scope.bc_id, scope.route.connection_id)
    )
    binding.status = "DISABLED"
    session.flush()
    progress = advance_composition(session, run["run_id"], run["generation"])
    assert progress["status"] == "FAILED"
    assert list_creative_units(session, scope).items == old


def test_candidate_snapshot_and_state_have_scoped_database_fences(
    session, image_draft, other_context
):
    from sqlalchemy.exc import IntegrityError

    from app.modules.builds.image_composition_state import (
        ImageCandidateExclusion,
        ImageCompositionState,
    )

    draft, drama, scope = image_draft
    state = session.get(ImageCompositionState, (scope.tenant_id, draft.id))
    with pytest.raises(IntegrityError), session.begin_nested():
        state.snapshot_id = uuid4()
        session.flush()
    foreign = material(session, other_context, "Moon.jpg")
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ImageCandidateExclusion(
                tenant_id=scope.tenant_id,
                draft_id=draft.id,
                bc_id=scope.bc_id,
                drama_id=drama.drama_id,
                material_id=foreign.id,
            )
        )
        session.flush()


def test_invalid_effective_drama_patch_does_not_change_revision(session, image_draft):
    draft, drama, scope = image_draft
    revision = draft.revision
    with pytest.raises(DomainError) as invalid:
        preview_composition(
            session,
            scope,
            uuid4(),
            revision,
            ImageRule(),
            {str(drama.drama_id): {"images_per_unit": 1}},
            "PRESERVE_MANUAL",
        )
    assert invalid.value.code == "image_edit_invalid" and draft.revision == revision


def test_missing_digest_and_disappeared_images_are_reported_never_shrunk(
    session, image_draft
):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import SetProtection
    from app.modules.materials.models import MaterialFile

    draft, _, scope = image_draft
    first = apply_rule(session, image_draft)[0]
    edit(
        session,
        image_draft,
        SetProtection(
            kind="SET_PROTECTION", unit_id=UUID(first["id"]), composition_protected=True
        ),
    )
    missing = session.get(MaterialFile, UUID(first["ordered_material_ids"][0]))
    missing.sha256 = None
    session.flush()
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert any(c["code"] == "IMAGE_UNAVAILABLE" for c in plan["conflicts"])
    assert (
        list_creative_units(session, scope).items[0]["ordered_material_ids"]
        == first["ordered_material_ids"]
    )
    snapshot = refresh_candidates(session, scope, uuid4(), draft.revision)
    snapshot = finish_candidate_snapshot(session, scope, snapshot)
    adopt_candidates(session, scope, uuid4(), draft.revision, snapshot["snapshot_id"])
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert any(c["code"] == "IMAGE_UNAVAILABLE" for c in plan["conflicts"])


def test_paginated_candidate_snapshot_is_frozen_across_name_changes(
    session, image_draft
):
    from app.modules.builds.image_composition_service import list_candidates

    draft, drama, scope = image_draft
    originals = []
    for index in range(7, 205):
        row = material(session, scope.context, f"Moon_{index}.jpg")
        row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
        originals.append(row)
    session.flush()
    snapshot = refresh_candidates(session, scope, uuid4(), draft.revision)
    snapshot = finish_candidate_snapshot(session, scope, snapshot)
    adopt_candidates(session, scope, uuid4(), draft.revision, snapshot["snapshot_id"])
    original_name = originals[0].file_name
    originals[0].file_name = "Renamed-after-snapshot.jpg"
    session.flush()
    cursor, results, sizes = None, [], []
    while True:
        page = list_candidates(session, scope, cursor=cursor, limit=100)
        results.extend(page.items)
        sizes.append(len(page.items))
        cursor = page.next_cursor
        if not cursor:
            break
    assert sizes == [100, 100, 5]
    assert len({r["material_id"] for r in results}) == 205
    assert (
        next(r for r in results if r["material_id"] == str(originals[0].id))[
            "file_name_snapshot"
        ]
        == original_name
    )


def test_revoked_route_progress_and_cancel_remain_publicly_available(
    client, session, context, image_draft
):
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User
    from app.modules.accounts.connection_models import BCConnectionBinding

    draft, _, scope = image_draft
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    binding = session.get(
        BCConnectionBinding, (scope.tenant_id, scope.bc_id, scope.route.connection_id)
    )
    binding.status = "DISABLED"
    session.flush()
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, context.actor_id
    )
    url = f"/api/tenants/{scope.tenant_id}/build-drafts/{draft.id}/image-composition/runs/{run['run_id']}"
    assert client.get(url).status_code == 200
    response = client.post(
        url + "/cancel",
        json={"request_id": str(uuid4()), "expected_revision": draft.revision},
    )
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"


@pytest.mark.parametrize(
    "rule,count,status",
    [
        (ImageRule(single_music_id="single"), 1, "PENDING"),
        (ImageRule(), 1, "NEEDS_SELECTION"),
        (ImageRule(), 2, "PENDING"),
        (ImageRule(music_mode="FIXED"), 2, "NEEDS_SELECTION"),
        (ImageRule(music_mode="FIXED", fixed_music_id="fixed"), 1, "PENDING"),
    ],
)
def test_manual_new_unit_carries_effective_music_rule_for_t3(
    session, image_draft, rule, count, status
):
    from uuid import UUID

    from app.modules.builds.image_composition import music_rule_digest
    from app.modules.builds.image_composition_service import list_candidates

    draft, drama, scope = image_draft
    preview_composition(
        session, scope, uuid4(), draft.revision, rule, {}, "PRESERVE_MANUAL"
    )
    candidates = list_candidates(session, scope).items
    edit(
        session,
        image_draft,
        Create(
            kind="CREATE",
            drama_id=drama.drama_id,
            material_ids=tuple(UUID(c["material_id"]) for c in candidates[:count]),
        ),
    )
    unit = list_creative_units(session, scope).items[0]
    assert unit["music_rule_digest"] == music_rule_digest(rule, count)
    assert unit["music_status"] == status and unit["music_id"] is None


def test_manual_add_first_freezes_authorized_candidate_without_reviving_exclusions(
    session, image_draft, other_context
):
    from app.modules.builds.image_composition_service import (
        add_candidate_images,
        exclude_candidates,
        list_candidates,
    )

    draft, drama, scope = image_draft
    extra = material(
        session, scope.context, "UnrelatedFilename.jpg", bc="other-upload-bc"
    )
    extra.media_type, extra.sha256 = "IMAGE", "c" * 64
    foreign = material(session, other_context, "Private.jpg")
    foreign.media_type, foreign.sha256 = "IMAGE", "b" * 64
    session.flush()
    with pytest.raises(DomainError):
        edit(
            session,
            image_draft,
            Create(kind="CREATE", drama_id=drama.drama_id, material_ids=(extra.id,)),
        )
    with pytest.raises(DomainError):
        add_candidate_images(
            session, scope, uuid4(), draft.revision, drama.drama_id, (foreign.id,)
        )
    result = add_candidate_images(
        session, scope, uuid4(), draft.revision, drama.drama_id, (extra.id,)
    )
    assert result["added"] == 1 and list_candidates(session, scope).total == 8
    edit(
        session,
        image_draft,
        Create(kind="CREATE", drama_id=drama.drama_id, material_ids=(extra.id,)),
    )
    exclude_candidates(
        session, scope, uuid4(), draft.revision, drama.drama_id, (extra.id,)
    )
    add_candidate_images(
        session, scope, uuid4(), draft.revision, drama.drama_id, (extra.id,)
    )
    assert (
        next(
            c
            for c in list_candidates(session, scope).items
            if c["material_id"] == str(extra.id)
        )["enabled"]
        is False
    )


def test_difference_pages_keep_excluded_images_out_of_plan_summary(
    session, image_draft
):
    from uuid import UUID

    from app.modules.builds.image_composition_service import (
        exclude_candidates,
        list_candidates,
        list_plan_differences,
    )

    draft, drama, scope = image_draft
    candidates = list_candidates(session, scope).items
    exclude_candidates(
        session,
        scope,
        uuid4(),
        draft.revision,
        drama.drama_id,
        tuple(UUID(c["material_id"]) for c in candidates),
    )
    plan = preview_composition(
        session, scope, uuid4(), draft.revision, ImageRule(), {}, "PRESERVE_MANUAL"
    )
    assert plan["unit_count"] == 0
    assert "excluded_images" not in plan["dramas"][0]
    assert plan["dramas"][0]["excluded_images_count"] == 7
    page = list_plan_differences(
        session, scope, plan["plan_id"], kind="excluded_images", limit=2
    )
    assert page.total == 7 and len(page.items) == 2 and page.next_cursor
    with pytest.raises(DomainError):
        list_plan_differences(
            session, scope, plan["plan_id"], kind="conflicts", cursor=page.next_cursor
        )


@pytest.mark.parametrize("staged", [False, True])
def test_worker_rechecks_build_role_before_staging_or_switch(
    session, image_draft, staged
):
    from app.modules.tenants.models import TenantMembership

    draft, _, scope = image_draft
    old = apply_rule(session, image_draft)
    old_generation = draft.active_image_generation
    plan = preview_composition(
        session,
        scope,
        uuid4(),
        draft.revision,
        ImageRule(images_per_unit=2),
        {},
        "PRESERVE_MANUAL",
    )
    run = apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    if staged:
        assert (
            advance_composition(session, run["run_id"], run["generation"], limit=1)[
                "status"
            ]
            == "PENDING"
        )
    member = session.get(TenantMembership, (scope.tenant_id, scope.actor_id))
    member.role = "viewer"
    session.flush()
    progress = advance_composition(session, run["run_id"], run["generation"])
    assert (
        progress["status"] == "FAILED" and progress["error_code"] == "action_forbidden"
    )
    assert draft.active_image_generation == old_generation
    assert list_creative_units(session, scope).items == old


def test_candidate_refresh_checkpoints_and_rejects_incomplete_adoption(
    session, image_draft
):
    from app.modules.builds.image_composition_service import list_candidates

    draft, _, scope = image_draft
    for index in range(7, 205):
        row = material(session, scope.context, f"Moon_{index}.jpg")
        row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
    session.flush()
    refresh = refresh_candidates(session, scope, uuid4(), draft.revision)
    assert refresh["status"] == "PENDING" and refresh["processed"] == 100
    with pytest.raises(DomainError) as hidden:
        list_candidates(session, scope, refresh["snapshot_id"])
    assert hidden.value.code == "image_candidates_incomplete"
    assert list_candidates(session, scope).total == 7
    with pytest.raises(DomainError) as incomplete:
        adopt_candidates(
            session, scope, uuid4(), draft.revision, refresh["snapshot_id"]
        )
    assert incomplete.value.code == "image_candidates_incomplete"


@pytest.mark.parametrize(
    "action,error",
    [
        ("role", "action_forbidden"),
        ("revision", "draft_revision_conflict"),
        ("cancel", "image_candidates_cancelled"),
    ],
)
def test_candidate_generation_stale_permission_and_cancel_preserve_active(
    session, image_draft, action, error
):
    from app.modules.builds.image_candidate_service import (
        advance_candidate_snapshot,
        cancel_candidate_snapshot,
        candidate_snapshot_progress,
    )
    from app.modules.builds.image_composition_service import list_candidates
    from app.modules.tenants.models import TenantMembership

    draft, _, scope = image_draft
    old = apply_rule(session, image_draft)
    active = draft.active_image_generation
    for index in range(7, 205):
        row = material(session, scope.context, f"Moon_{index}.jpg")
        row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
    session.flush()
    refresh = refresh_candidates(session, scope, uuid4(), draft.revision)
    assert refresh["status"] == "PENDING"
    assert (
        advance_candidate_snapshot(
            session, refresh["snapshot_id"], refresh["generation"] - 1
        )
        == refresh
    )
    if action == "role":
        session.get(TenantMembership, (scope.tenant_id, scope.actor_id)).role = "viewer"
    elif action == "revision":
        draft.revision += 1
    else:
        cancel_candidate_snapshot(
            session, scope, refresh["snapshot_id"], uuid4(), draft.revision
        )
    session.flush()
    result = advance_candidate_snapshot(
        session, refresh["snapshot_id"], refresh["generation"]
    )
    assert result["status"] == ("CANCELLED" if action == "cancel" else "FAILED")
    assert result["processed"] == 100 and result["error_code"] == error
    assert candidate_snapshot_progress(session, scope, refresh["snapshot_id"]) == result
    assert (
        draft.active_image_generation == active
        and list_creative_units(session, scope).items == old
    )
    assert list_candidates(session, scope).total == 7


def test_candidate_worker_rollback_replay_recovery_retains_cutoff(
    committed_image_draft,
):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from sqlmodel import Session

    from app.jobs.models import PendingDispatch
    from app.modules.builds.image_candidate_service import (
        advance_candidate_snapshot,
        candidate_snapshot_progress,
    )
    from app.modules.builds.image_candidate_tasks import (
        process_candidate_snapshot,
        repair_candidate_snapshots,
    )
    from app.modules.builds.image_composition_service import list_candidates
    from app.modules.builds.image_composition_state import (
        ImageCandidateItem,
        ImageCandidateSnapshot,
    )

    scope, _, engine = committed_image_draft
    with Session(engine) as setup:
        draft = setup.get(BuildDraft, scope.draft_id)
        for index in range(61, 261):
            row = material(setup, scope.context, f"Moon_{index}.jpg")
            row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
        setup.flush()
        first = refresh_candidates(setup, scope, uuid4(), draft.revision)
        assert first["processed"] == 100
        setup.commit()
    with Session(engine) as crashed:
        result = advance_candidate_snapshot(
            crashed, first["snapshot_id"], first["generation"]
        )
        assert result["processed"] == 200
        crashed.rollback()
    with Session(engine) as check:
        snapshot = check.get(ImageCandidateSnapshot, UUID(first["snapshot_id"]))
        assert (
            snapshot.candidate_count == 100
            and snapshot.generation == first["generation"]
        )
        assert (
            len(
                check.exec(
                    select(ImageCandidateItem).where(
                        ImageCandidateItem.snapshot_id == snapshot.id
                    )
                ).all()
            )
            == 100
        )
        late = material(check, scope.context, "Moon_late.jpg")
        late.media_type, late.sha256 = "IMAGE", "f" * 64
        late_id = late.id
        dispatch = check.get(PendingDispatch, snapshot.dispatch_id)
        payload, original_dispatch_id = dict(dispatch.payload), dispatch.id
        snapshot.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        dispatch.published_at = datetime.now(UTC)
        check.commit()
    assert repair_candidate_snapshots(database_engine=engine) == 1
    with Session(engine) as check:
        snapshot = check.get(ImageCandidateSnapshot, UUID(first["snapshot_id"]))
        assert snapshot.dispatch_id == original_dispatch_id
        assert check.get(PendingDispatch, original_dispatch_id).published_at is None
    for _ in range(2):
        process_candidate_snapshot(
            database_engine=engine,
            tenant_id=scope.tenant_id,
            actor_id=scope.actor_id,
            payload=payload,
        )
    with Session(engine) as check:
        snapshot = check.get(ImageCandidateSnapshot, UUID(first["snapshot_id"]))
        assert snapshot.candidate_count == 200
        payload = dict(check.get(PendingDispatch, snapshot.dispatch_id).payload)
    process_candidate_snapshot(
        database_engine=engine,
        tenant_id=scope.tenant_id,
        actor_id=scope.actor_id,
        payload=payload,
    )
    with Session(engine) as check:
        result = candidate_snapshot_progress(check, scope, first["snapshot_id"])
        assert result["status"] == "READY" and result["candidate_count"] == 261
        assert (
            result["added"] == 200
            and result["removed"] == 0
            and result["cutoff"] == first["cutoff"]
        )
        assert list_candidates(check, scope).total == 61
        assert not check.exec(
            select(ImageCandidateItem).where(
                ImageCandidateItem.snapshot_id == UUID(first["snapshot_id"]),
                ImageCandidateItem.material_id == late_id,
            )
        ).all()
        draft = check.get(BuildDraft, scope.draft_id)
        adopt_candidates(check, scope, uuid4(), draft.revision, first["snapshot_id"])
        assert list_candidates(check, scope).total == 261
        check.commit()


def test_candidate_progress_cancel_api_and_tenant_scope(
    client, session, context, image_draft
):
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User

    draft, _, scope = image_draft
    for index in range(7, 105):
        row = material(session, scope.context, f"Moon_{index}.jpg")
        row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
    session.flush()
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, context.actor_id
    )
    url = f"/api/tenants/{scope.tenant_id}/build-drafts/{draft.id}/image-composition/candidates"
    response = client.post(
        url + "/refresh",
        json={"request_id": str(uuid4()), "expected_revision": draft.revision},
    )
    assert response.status_code == 200 and response.json()["status"] == "PENDING"
    snapshot_id = response.json()["snapshot_id"]
    assert client.get(url + f"/snapshots/{snapshot_id}").json()["processed"] == 100
    assert client.get(url + f"?snapshot_id={snapshot_id}").status_code == 409
    assert client.get(url + f"/snapshots/{uuid4()}").status_code == 404
    response = client.post(
        url + f"/snapshots/{snapshot_id}/cancel",
        json={"request_id": str(uuid4()), "expected_revision": draft.revision},
    )
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"


@pytest.mark.parametrize("remove", [False, True])
def test_edit_existing_duplicate_preserves_reuse_but_new_reuse_needs_consent(
    session, image_draft, remove
):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import Duplicate, SetImages

    _, _, scope = image_draft
    first, second, _ = apply_rule(session, image_draft)
    original = UUID(first["id"])
    ids = tuple(map(UUID, first["ordered_material_ids"]))
    clone = UUID(
        edit(session, image_draft, Duplicate(kind="DUPLICATE", unit_id=original))[
            "affected_units"
        ][0]
    )
    changed = tuple(reversed(ids))[1:] if remove else tuple(reversed(ids))
    edit(
        session,
        image_draft,
        SetImages(kind="SET_IMAGES", unit_id=clone, material_ids=changed),
    )
    rows = {u["id"]: u for u in list_creative_units(session, scope).items}
    assert rows[str(clone)]["ordered_material_ids"] == list(map(str, changed))
    assert rows[str(original)]["ordered_material_ids"] == list(map(str, ids))
    # 已确认的交集可继续编辑，但其他轮播中的新图片仍须明确同意。
    new_image = UUID(second["ordered_material_ids"][0])
    with pytest.raises(DomainError) as denied:
        edit(
            session,
            image_draft,
            SetImages(
                kind="SET_IMAGES", unit_id=clone, material_ids=(*changed, new_image)
            ),
        )
    assert denied.value.code == "image_reuse_confirmation_required"
    edit(
        session,
        image_draft,
        SetImages(
            kind="SET_IMAGES",
            unit_id=clone,
            material_ids=(*changed, new_image),
            allow_reuse=True,
        ),
    )
