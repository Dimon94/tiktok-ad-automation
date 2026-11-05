from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.builds.draft_tasks import process_preparation, repair_preparations
from app.modules.builds.drafts import continue_draft, create_draft, prepare_draft
from app.modules.builds.models import (
    BuildDraft,
    DraftDrama,
    DraftPreparation,
)
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_drafts import account, ready_links
from tests.modules.builds.video_source import VIDEO_SOURCE
from tests.modules.materials.test_tenant_materials import material


def material_task(session, context, intent, titles):
    intent = {**intent, "drama_lines": titles}
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    ready_links(session, context, task, intent)
    for _ in range(10):
        prep = session.get(DraftPreparation, task)
        if prep.phase == "materials":
            return draft, task
        continue_draft(session, context=context, task_id=task)
    raise AssertionError("materials phase not reached")


def test_material_batch_yields_without_delay_and_duplicate_delivery_is_noop(
    session, context, intent, monkeypatch
):
    from app.modules.builds import draft_tasks

    monkeypatch.setattr(draft_tasks, "monotonic", lambda: 0.0)
    draft, task = material_task(
        session, context, intent, [f"Drama {i}" for i in range(22)]
    )
    payload = {"preparation_id": str(task), "generation": 0}
    for _ in range(2):
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload=payload,
        )
    session.expire_all()
    rows = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).all()
    assert sum(row.material_state == "ready" for row in rows) == 5
    prep = session.get(DraftPreparation, task)
    assert prep.generation == 1
    assert prep.due_at <= datetime.now(UTC)
    for generation in range(1, 6):
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload={"preparation_id": str(task), "generation": generation},
        )
    session.expire_all()
    assert session.get(BuildDraft, draft).status == "READY"


def test_material_batch_time_budget_checkpoints_after_current_page(
    session, context, intent, monkeypatch
):
    from app.modules.builds import draft_tasks

    times = iter([0.0, 2.0])
    monkeypatch.setattr(draft_tasks, "monotonic", lambda: next(times))
    draft, task = material_task(session, context, intent, ["Moon", "Stars"])
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task), "generation": 0},
    )
    session.expire_all()
    rows = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).all()
    assert sum(row.material_state == "ready" for row in rows) == 1
    assert session.get(DraftPreparation, task).due_at <= datetime.now(UTC)


def test_twenty_two_dramas_match_without_scheduled_sleeps(
    session, context, intent, record_property
):
    titles = [f"Drama-{i:03}-Title" for i in range(22)]
    draft, task = material_task(session, context, intent, titles)
    for title in titles:
        for i in range(25):
            material(session, context, f"{title}-{i:03}.mp4", bc="bc-draft")
    session.flush()
    started = perf_counter()
    for turns in range(1, 24):
        record_property("matching_turn", turns)
        prep = session.get(DraftPreparation, task)
        assert prep.due_at <= datetime.now(UTC)
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload={"preparation_id": str(task), "generation": prep.generation},
        )
        session.expire_all()
        rows = session.exec(
            select(DraftDrama).where(DraftDrama.draft_id == draft)
        ).all()
        if all(row.material_state == "ready" for row in rows):
            break
    else:
        raise AssertionError("material matching did not finish")
    assert all(row.matched_count == 25 for row in rows)
    selected = session.exec(
        select(*VIDEO_SOURCE.c).where(VIDEO_SOURCE.c.draft_id == draft)
    ).all()
    assert len(selected) == 550
    record_property("matching_seconds", perf_counter() - started)


def test_material_batch_preserves_all_pages_and_selection_positions(
    session, context, intent, monkeypatch
):
    from app.modules.builds import draft_tasks

    monkeypatch.setattr(draft_tasks, "monotonic", lambda: 0.0)
    draft, task = material_task(session, context, intent, ["Moon"])
    expected = [
        material(session, context, f"Moon-{i:03}.mp4", bc="bc-draft")
        for i in range(205)
    ]
    session.flush()
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task), "generation": 0},
    )
    session.expire_all()
    selected = session.exec(
        select(*VIDEO_SOURCE.c).where(VIDEO_SOURCE.c.draft_id == draft)
    ).all()
    assert {row.material_id for row in selected} == {row.id for row in expected}
    assert len(selected) == 205
    assert len({(row.group_no, row.position) for row in selected}) == 205
    assert session.get(DraftPreparation, task).due_at <= datetime.now(UTC)


def test_duplicate_delivery_only_advances_one_local_page(session, context, intent):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    payload = {"preparation_id": str(task), "generation": 0}
    for _ in range(2):
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload=payload,
        )
    session.expire_all()
    prep = session.get(DraftPreparation, task)
    assert prep.generation == 1 and prep.account_after == 4 and prep.phase == "accounts"
    assert prep.due_at <= datetime.now(UTC)


def test_local_progress_and_phase_changes_continue_without_scheduled_sleeps(
    session, context, intent
):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    ready_links(session, context, task, intent)
    for generation, phase in enumerate(("accounts", "links", "materials")):
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload={"preparation_id": str(task), "generation": generation},
        )
        session.expire_all()
        prep = session.get(DraftPreparation, task)
        assert prep.phase == phase
        assert prep.due_at <= datetime.now(UTC)


def test_external_scene_wait_keeps_backoff(session, context, intent, monkeypatch):
    from app.modules.builds import drafts

    draft, task = material_task(session, context, intent, ["Moon"])
    row = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).one()
    row.material_state = "ready"
    session.add(row)
    session.flush()
    calls = []
    # 模拟场景依赖尚未就绪，验证本地批处理不能把外部等待变成忙轮询。
    monkeypatch.setattr(drafts, "_scenes_page", lambda *args: calls.append(True))
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task), "generation": 0},
    )
    session.expire_all()
    assert calls == [True]
    assert session.get(DraftPreparation, task).due_at > datetime.now(UTC) + timedelta(
        seconds=3
    )


def test_material_batch_error_rolls_back_partial_selection(
    session, context, intent, monkeypatch
):
    from app.core.errors import DomainError
    from app.modules.builds import draft_tasks, drafts

    monkeypatch.setattr(draft_tasks, "monotonic", lambda: 0.0)
    draft, task = material_task(session, context, intent, ["Moon", "Stars"])
    material(session, context, "Moon-1.mp4", bc="bc-draft")
    session.flush()
    original = drafts.match_materials

    def fail_second(*args, **kwargs):
        if kwargs["title"] == "Stars":
            raise DomainError("invalid_cursor", "injected failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(drafts, "match_materials", fail_second)
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task), "generation": 0},
    )
    session.expire_all()
    assert session.get(DraftPreparation, task).status == "BLOCKED"
    assert not session.exec(
        select(*VIDEO_SOURCE.c).where(VIDEO_SOURCE.c.draft_id == draft)
    ).all()
    assert all(
        row.material_state == "pending"
        for row in session.exec(
            select(DraftDrama).where(DraftDrama.draft_id == draft)
        ).all()
    )


def test_revoked_actor_becomes_explicitly_blocked_and_can_prepare_after_restore(
    session, context, intent
):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    member = session.exec(
        select(TenantMembership).where(
            TenantMembership.tenant_id == context.tenant_id,
            TenantMembership.user_id == context.actor_id,
        )
    ).one()
    member.role = "viewer"
    session.add(member)
    session.flush()
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task), "generation": 0},
    )
    session.expire_all()
    assert session.get(DraftPreparation, task).status == "BLOCKED"
    assert session.get(DraftPreparation, task).error_code == "action_forbidden"
    assert session.get(BuildDraft, draft).status == "BLOCKED"
    member.role = "operator"
    session.add(member)
    session.flush()
    retry = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    assert retry != task
    assert (
        session.get(DraftPreparation, retry).provider_task_id
        == session.get(DraftPreparation, task).provider_task_id
    )


def test_repair_preserves_current_identity_and_unpublished_backoff(
    session, context, intent
):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    prep = session.get(DraftPreparation, task)
    dispatch = session.get(PendingDispatch, prep.dispatch_id)
    original = dispatch.id
    future = datetime.now(UTC) + timedelta(minutes=10)
    prep.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    dispatch.available_at = future
    session.add_all([prep, dispatch])
    session.flush()
    assert repair_preparations(database_engine=session.connection(), limit=1) == 1
    session.expire_all()
    assert session.get(PendingDispatch, original).available_at == future
    prep = session.get(DraftPreparation, task)
    dispatch = session.get(PendingDispatch, original)
    prep.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    dispatch.published_at = datetime.now(UTC) - timedelta(minutes=5)
    session.add_all([prep, dispatch])
    session.flush()
    assert repair_preparations(database_engine=session.connection(), limit=1) == 1
    session.expire_all()
    assert session.get(DraftPreparation, task).dispatch_id == original
    assert session.get(DraftPreparation, task).generation == 0
    assert session.get(PendingDispatch, original).published_at is None


def test_carousel_initial_candidates_checkpoint_within_preparation_budget(
    session, context, intent
):
    import pytest

    from app.modules.builds.image_composition_service import (
        list_candidates,
        resolve_image_scope,
    )
    from app.modules.builds.image_composition_state import (
        ImageCandidateItem,
        ImageCandidateSnapshot,
        ImageCompositionState,
    )

    draft_id, task_id = material_task(session, context, intent, ["Moon"])
    draft = session.get(BuildDraft, draft_id)
    draft.media_kind = "CAROUSEL"
    for index in range(1501):
        row = material(session, context, f"Moon_{index:04}.jpg")
        row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
    session.flush()
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task_id), "generation": 0},
    )
    session.expire_all()
    snapshot = session.exec(
        select(ImageCandidateSnapshot).where(
            ImageCandidateSnapshot.preparation_id == task_id
        )
    ).one()
    assert snapshot.status == "PENDING" and 100 <= snapshot.candidate_count <= 500
    assert snapshot.candidate_count == snapshot.generation * 100
    state = session.get(ImageCompositionState, (context.tenant_id, draft_id))
    assert state.snapshot_id is None
    identity, count, cursor, cutoff = (
        snapshot.id,
        snapshot.candidate_count,
        snapshot.generation,
        snapshot.created_at,
    )
    # 一个随后中断的页面真实回滚；上一轮已提交的快照和游标保留。
    with pytest.raises(RuntimeError), session.begin_nested():
        continue_draft(session, context=context, task_id=task_id)
        assert snapshot.candidate_count == count + 100
        raise RuntimeError("worker interrupted before commit")
    session.expire_all()
    snapshot = session.get(ImageCandidateSnapshot, identity)
    assert snapshot.candidate_count == count and snapshot.generation == cursor
    assert (
        len(
            session.exec(
                select(ImageCandidateItem).where(
                    ImageCandidateItem.snapshot_id == identity
                )
            ).all()
        )
        == count
    )
    for _ in range(25):
        prep = session.get(DraftPreparation, task_id)
        if prep.status != "PENDING":
            break
        payload = {"preparation_id": str(task_id), "generation": prep.generation}
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload=payload,
        )
        # 原投递重放不能重复处理已提交的页。
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload=payload,
        )
        session.expire_all()
    assert session.get(DraftPreparation, task_id).status == "READY"
    snapshot = session.get(ImageCandidateSnapshot, identity)
    assert (
        snapshot.status == "READY"
        and snapshot.candidate_count == 1501
        and snapshot.created_at == cutoff
    )
    assert (
        session.get(ImageCompositionState, (context.tenant_id, draft_id)).snapshot_id
        == identity
    )
    scope = resolve_image_scope(session, context=context, draft_id=draft_id)
    assert list_candidates(session, scope).total == 1501


def test_carousel_initial_candidates_role_loss_fails_preparation_and_snapshot(
    session, context, intent
):
    from app.modules.builds.image_composition_state import (
        ImageCandidateSnapshot,
        ImageCompositionState,
    )

    draft_id, task_id = material_task(session, context, intent, ["Moon"])
    session.get(BuildDraft, draft_id).media_kind = "CAROUSEL"
    for index in range(151):
        row = material(session, context, f"Moon_{index:04}.jpg")
        row.media_type, row.sha256 = "IMAGE", f"{index:064x}"
    session.flush()
    continue_draft(session, context=context, task_id=task_id)
    snapshot = session.exec(
        select(ImageCandidateSnapshot).where(
            ImageCandidateSnapshot.preparation_id == task_id
        )
    ).one()
    identity = snapshot.id
    assert snapshot.candidate_count == 100 and snapshot.status == "PENDING"
    session.get(TenantMembership, (context.tenant_id, context.actor_id)).role = "viewer"
    session.flush()
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task_id), "generation": 0},
    )
    session.expire_all()
    assert session.get(DraftPreparation, task_id).status == "BLOCKED"
    snapshot = session.get(ImageCandidateSnapshot, identity)
    assert snapshot.status == "FAILED" and snapshot.error_code == "action_forbidden"
    assert snapshot.candidate_count == 100
    assert (
        session.get(ImageCompositionState, (context.tenant_id, draft_id)).snapshot_id
        is None
    )
