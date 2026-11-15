"""Independent review regressions for draft preparation identity across edits."""

from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.builds.drafts import (
    create_draft,
    edit_material_groups,
    prepare_draft,
    update_draft,
)
from app.modules.builds.models import DraftDrama, DraftInput, DraftPreparation
from app.modules.providers.models import LinkPreparationItem
from tests.modules.builds.test_drafts import account, finish, ready_links
from tests.modules.builds.video_source import VIDEO_SOURCE
from tests.modules.materials.test_tenant_materials import material


@pytest.mark.parametrize("edit_kind", ["manual_materials", "account_lines"])
def test_non_provider_edit_preserves_unknown_provider_preparation(
    session, context, intent, edit_kind
):
    account(session, context)
    file = material(session, context, "Moon.mp4", bc="bc-draft")
    draft_id = create_draft(session, context=context, **intent)
    first = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, first, intent)
    first_provider = session.get(DraftPreparation, first).provider_task_id
    unknown = session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == first_provider,
            LinkPreparationItem.line_no == 4,
        )
    ).one()
    unknown.status = "result_unknown"
    unknown.resolved = {
        **unknown.resolved,
        "status": "result_unknown",
        "error_code": "provider_state_invalid"
        if edit_kind == "manual_materials"
        else "provider_result_unknown",
        "_work": {
            **unknown.resolved.get("_work", {}),
            "stage": "invalid" if edit_kind == "manual_materials" else "recover_create",
        },
    }
    session.add(unknown)
    session.flush()
    from app.modules.builds.drafts import continue_draft

    if edit_kind == "manual_materials":
        # 素材编辑只允许已结束准备的草稿；隔离任务保留Unknown供人工处理。
        finish(session, context, first)
    else:
        for _ in range(20):
            assert not continue_draft(session, context=context, task_id=first)
    if edit_kind == "manual_materials":
        drama = session.exec(
            select(DraftDrama).where(DraftDrama.draft_id == draft_id)
        ).one()
        edit_material_groups(
            session,
            context=context,
            draft_id=draft_id,
            drama_id=drama.drama_id,
            expected_revision=1,
            groups=[[file.id]],
        )
    else:
        update_draft(
            session,
            context=context,
            draft_id=draft_id,
            expected_revision=1,
            account_lines=["account-A"],
        )
    refreshed = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert session.get(DraftPreparation, refreshed).provider_task_id == first_provider


def test_manual_edit_then_refresh_does_not_mark_first_account_duplicate_of_itself(
    session, context, intent
):
    account(session, context)
    file = material(session, context, "Moon.mp4", bc="bc-draft")
    draft_id = create_draft(session, context=context, **intent)
    first = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, first, intent)
    finish(session, context, first)
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == draft_id)
    ).first()
    edit_material_groups(
        session,
        context=context,
        draft_id=draft_id,
        drama_id=drama.drama_id,
        expected_revision=1,
        groups=[[file.id]],
    )
    refreshed = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    from app.modules.builds.drafts import continue_draft

    continue_draft(session, context=context, task_id=refreshed)
    row = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft_id,
            DraftInput.kind == "account",
            DraftInput.line_no == 1,
        )
    ).one()
    assert (row.status, row.duplicate_of) == ("matched", None)


def test_revoked_obsolete_worker_cannot_block_new_draft_revision(
    session, context, intent
):
    from app.modules.builds.draft_tasks import process_preparation
    from app.modules.builds.models import BuildDraft
    from app.modules.tenants.models import TenantMembership

    account(session, context)
    draft_id = create_draft(session, context=context, **intent)
    old_task = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    update_draft(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=1,
        account_lines=["new-account"],
    )
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.add(member)
    session.flush()
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(old_task), "generation": 0},
    )
    session.expire_all()
    assert session.get(BuildDraft, draft_id).status == "DRAFT"
    assert session.get(DraftPreparation, old_task).status == "OBSOLETE"


def test_aliases_merge_to_same_drama_and_accounts_resolve_full_name_exactly(
    session, context, intent
):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.builds.models import DraftAccount

    account(session, context)
    account_row = session.get(AdvertiserAccount, (context.tenant_id, "account-A"))
    account_row.name = "Exact Full Account"
    session.add(account_row)
    draft_id = create_draft(
        session,
        context=context,
        **(
            intent
            | {"account_lines": ["Exact Full Account", "account-A", "Full Account"]}
        ),
    )
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, task_id, intent)
    prep = session.get(DraftPreparation, task_id)
    provider_rows = session.exec(
        select(LinkPreparationItem)
        .where(LinkPreparationItem.preparation_id == prep.provider_task_id)
        .order_by(LinkPreparationItem.line_no)
    ).all()
    first, alias = provider_rows
    for key in [
        "drama_id",
        "external_drama_id",
        "title",
        "link_id",
        "url",
        "protected_base",
    ]:
        alias.resolved = {**alias.resolved, key: first.resolved[key]}
    session.add(alias)
    session.flush()
    finish(session, context, task_id)
    drama_rows = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == draft_id)
    ).all()
    assert len(drama_rows) == 1
    inputs = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft_id,
            DraftInput.kind == "drama",
            DraftInput.line_no == 4,
        )
    ).one()
    assert (
        inputs.raw_text == "Short Drama"
        and inputs.status == "duplicate"
        and inputs.duplicate_of == 1
    )
    accounts = session.exec(
        select(DraftInput)
        .where(DraftInput.draft_id == draft_id, DraftInput.kind == "account")
        .order_by(DraftInput.line_no)
    ).all()
    assert [(r.status, r.advertiser_id) for r in accounts] == [
        ("matched", "account-A"),
        ("duplicate", "account-A"),
        ("not_found", None),
    ]
    assert (
        len(
            session.exec(
                select(DraftAccount).where(DraftAccount.draft_id == draft_id)
            ).all()
        )
        == 1
    )


def test_large_account_input_advances_one_page_without_combination_expansion(
    session, context, intent
):
    from app.modules.builds.drafts import continue_draft
    from app.modules.builds.models import DraftAccount

    account(session, context)
    values = ["account-A"] + ["missing"] * 2000
    draft_id = create_draft(
        session, context=context, **(intent | {"account_lines": values})
    )
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert not continue_draft(session, context=context, task_id=task_id)
    prep = session.get(DraftPreparation, task_id)
    assert prep.account_after == 100 and prep.phase == "accounts"
    assert (
        len(
            session.exec(
                select(DraftAccount).where(DraftAccount.draft_id == draft_id)
            ).all()
        )
        == 1
    )
    untouched = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft_id,
            DraftInput.kind == "account",
            DraftInput.line_no == 2001,
        )
    ).one()
    assert untouched.advertiser_id is None


def test_refresh_preserves_manual_groups_rebuilds_auto_and_new_provider_config_gets_new_task(
    session, context, intent
):
    from app.modules.builds.models import BuildDraft

    keep = material(session, context, "Moon keep.mp4", bc="bc-draft")
    material(session, context, "Short Drama old.mp4", bc="bc-draft")
    account(session, context)
    draft_id = create_draft(session, context=context, **intent)
    first = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, first, intent)
    finish(session, context, first)
    rows = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft_id)).all()
    moon = next(row for row in rows if row.title == "Moon")
    short = next(row for row in rows if row.title == "Short Drama")
    edit_material_groups(
        session,
        context=context,
        draft_id=draft_id,
        drama_id=moon.drama_id,
        expected_revision=1,
        groups=[[keep.id]],
    )
    material(session, context, "Moon newly added.mp4", bc="bc-draft")
    material(session, context, "Short Drama newly added.mp4", bc="bc-draft")
    refreshed = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert session.get(BuildDraft, draft_id).revision == 3
    assert (
        session.get(DraftPreparation, refreshed).provider_task_id
        == session.get(DraftPreparation, first).provider_task_id
    )
    finish(session, context, refreshed)
    selected = session.exec(
        select(*VIDEO_SOURCE.c).where(VIDEO_SOURCE.c.draft_id == draft_id)
    ).all()
    assert [row.material_id for row in selected if row.drama_id == moon.drama_id] == [
        keep.id
    ]
    assert sum(row.drama_id == short.drama_id for row in selected) == 2
    assert (
        session.get(
            DraftDrama, (context.tenant_id, draft_id, moon.drama_id)
        ).material_state
        == "manual"
    )
    update_draft(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=3,
        link_config={**intent["link_config"], "episode": 2},
    )
    changed = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert (
        session.get(DraftPreparation, changed).provider_task_id
        != session.get(DraftPreparation, first).provider_task_id
    )
