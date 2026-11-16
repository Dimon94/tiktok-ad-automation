"""Real PostgreSQL upgrade and scoped creative membership, without video dual writes."""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds.drafts import (
    continue_draft,
    create_draft,
    edit_material_groups,
    prepare_draft,
)
from app.modules.builds.image_composition_models import (
    DraftCreativeUnit,
    DraftCreativeUnitItem,
)
from app.modules.builds.models import (
    BuildDraft,
    DraftDrama,
    DraftGroupMaterial,
    DraftPreparation,
)
from tests.migration_database import historical_database, insert_historical_row
from tests.modules.builds.test_drafts import account, create_intent, finish, ready_links
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import material


def test_upgrade_defaults_old_drafts_without_creating_stale_video_mirror(monkeypatch):
    with historical_database(monkeypatch, "0048_image_reissue_routes") as (
        engine,
        config,
    ):
        with Session(engine) as session:
            context = create_context(session)
            intent = create_intent(session, context, historical=True)
            draft = BuildDraft(
                tenant_id=context.tenant_id,
                created_by=context.actor_id,
                request_id=uuid4(),
                request_digest="a" * 64,
                **{
                    k: v
                    for k, v in intent.items()
                    if k not in {"drama_lines", "account_lines"}
                },
            )
            table = Table("build_draft", MetaData(), autoload_with=session.connection())
            session.execute(
                table.insert().values(
                    **{
                        k: v
                        for k, v in draft.model_dump().items()
                        if k in table.c and v is not None
                    }
                )
            )
            session.commit()
            identity, request = draft.id, draft.request_id
        command.upgrade(config, "0049_image_composition")
        with Session(engine) as session:
            table = Table("build_draft", MetaData(), autoload_with=session.connection())
            row = session.execute(table.select().where(table.c.id == identity)).one()
            assert (
                row.media_kind,
                row.active_image_generation,
                row.applied_image_rule_revision,
            ) == ("VIDEO", 0, None)
            assert row.request_id == request and row.request_digest == "a" * 64
            assert not session.exec(select(DraftCreativeUnit)).all()


def scaffold(session, context, intent):
    account(session, context)
    identity = create_draft(session, context=context, **intent)
    task = prepare_draft(
        session, context=context, draft_id=identity, request_id=uuid4()
    )
    ready_links(session, context, task, intent)
    finish(session, context, task)
    draft = session.get(BuildDraft, identity)
    draft.media_kind = "CAROUSEL"
    session.flush()
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == identity)
    ).first()
    return draft, drama


def historical_scaffold(
    session, context, intent, *, media_kind="CAROUSEL", active_image_generation=0
):
    """播种迁移输入事实，不让今日准备队列/派发 ORM 读取旧库中尚不存在的列。"""
    from datetime import UTC, datetime

    from app.modules.providers.models import PromotionLink, ProviderDrama
    from app.modules.providers.repository import link_reuse_key

    account(session, context, historical=True)
    draft = BuildDraft(
        tenant_id=context.tenant_id,
        created_by=context.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        media_kind=media_kind,
        active_image_generation=active_image_generation,
        status="READY",
        **{
            key: value
            for key, value in intent.items()
            if key not in {"drama_lines", "account_lines"}
        },
    )
    insert_historical_row(session, draft)
    title = next(line.strip() for line in intent["drama_lines"] if line.strip())
    provider_drama = ProviderDrama(
        tenant_id=context.tenant_id,
        connection_id=intent["provider_connection_id"],
        application_id=intent["application_id"],
        external_drama_id="1",
        display_drama_id="1",
        title=title,
    )
    session.add(provider_drama)
    session.flush()
    link = PromotionLink(
        tenant_id=context.tenant_id,
        connection_id=intent["provider_connection_id"],
        application_id=intent["application_id"],
        drama_id=provider_drama.id,
        reuse_key=link_reuse_key(
            context.tenant_id,
            intent["provider_connection_id"],
            intent["application_id"],
            "1",
            intent["link_config"],
        ),
        config=intent["link_config"],
        url="https://example.com/drama",
        protected_base=title,
        status="ready",
        verified_at=datetime.now(UTC),
    )
    session.add(link)
    session.flush()
    drama = DraftDrama(
        tenant_id=context.tenant_id,
        draft_id=draft.id,
        bc_id=draft.bc_id,
        drama_id=provider_drama.id,
        link_id=link.id,
        title=title,
        first_line=1,
        material_state="ready",
    )
    session.add(drama)
    session.flush()
    return draft, drama


def unit(draft, drama, **overrides):
    return DraftCreativeUnit(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        bc_id=draft.bc_id,
        drama_id=drama.drama_id,
        generation=1,
        kind="CAROUSEL",
        music_status="PENDING",
        position=1,
        content_digest="a" * 64,
        **overrides,
    )


def member(unit, material_id, position=1):
    return DraftCreativeUnitItem(
        tenant_id=unit.tenant_id,
        draft_id=unit.draft_id,
        bc_id=unit.bc_id,
        drama_id=unit.drama_id,
        generation=unit.generation,
        unit_id=unit.id,
        material_id=material_id,
        kind=unit.kind,
        media_type="IMAGE" if unit.kind == "CAROUSEL" else "VIDEO",
        position=position,
    )


def test_membership_reuses_images_across_units_but_not_within_unit(
    session, context, intent
):
    draft, drama = scaffold(session, context, intent)
    image = material(session, context, "image.jpg", bc="bc-draft")
    image.media_type = "IMAGE"
    first = unit(draft, drama)
    second = unit(draft, drama)
    second.position = 2
    session.add_all([first, second])
    session.flush()
    session.add_all([member(first, image.id), member(second, image.id)])
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(member(first, image.id, 2))
        session.flush()
    # 相同 identity 在暂存 generation 仍可存在；内容摘要不充当实体唯一键。
    staged = unit(draft, drama, id=first.id)
    staged.generation = 2
    session.add(staged)
    session.flush()
    assert (
        len(
            session.exec(
                select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == draft.id)
            ).all()
        )
        == 3
    )


def test_unit_foreign_keys_reject_wrong_tenant_bc_drama_and_media_kind(
    session, context, other_context, intent
):
    draft, drama = scaffold(session, context, intent)
    for updates in (
        {"tenant_id": other_context.tenant_id},
        {"bc_id": "other"},
        {"drama_id": uuid4()},
        {"kind": "VIDEO"},
    ):
        with pytest.raises(IntegrityError), session.begin_nested():
            row = unit(draft, drama)
            for key, value in updates.items():
                setattr(row, key, value)
            session.add(row)
            session.flush()
    row = unit(draft, drama)
    session.add(row)
    session.flush()
    foreign = material(session, other_context, "foreign.jpg")
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(member(row, foreign.id))
        session.flush()
    local = material(session, context, "local.jpg")
    with pytest.raises(IntegrityError), session.begin_nested():
        wrong_scope = member(row, local.id)
        wrong_scope.bc_id = "other"
        session.add(wrong_scope)
        session.flush()


def test_media_kind_is_part_of_creation_identity_and_blocks_video_writes(
    session, context, intent
):
    account(session, context)
    request = uuid4()
    video = create_draft(session, context=context, request_id=request, **intent)
    assert (
        create_draft(
            session, context=context, request_id=request, media_kind="VIDEO", **intent
        )
        == video
    )
    with pytest.raises(DomainError, match="同一请求"):
        create_draft(
            session,
            context=context,
            request_id=request,
            media_kind="CAROUSEL",
            **intent,
        )
    identity = create_draft(session, context=context, media_kind="CAROUSEL", **intent)
    assert session.get(BuildDraft, identity).media_kind == "CAROUSEL"
    from app.modules.builds.catalog import draft_summary
    from app.modules.builds.draft_catalog import list_drafts

    assert (
        draft_summary(session, context=context, draft_id=identity).media_kind
        == "CAROUSEL"
    )
    listed = list_drafts(session, context=context, bc_id=intent["bc_id"])
    assert {row.draft_id: row.media_kind for row in listed.items} == {
        video: "VIDEO",
        identity: "CAROUSEL",
    }
    # 中性账户/剧目准备仍可执行；视频素材阶段不能写入图片草稿。
    task = prepare_draft(
        session, context=context, draft_id=identity, request_id=uuid4()
    )
    ready_links(session, context, task, intent)
    for _ in range(20):
        if session.get(DraftPreparation, task).phase == "materials":
            break
        continue_draft(session, context=context, task_id=task)
    from app.modules.builds.drafts import _materials_page

    with pytest.raises(
        DomainError, check=lambda e: e.code == "draft_media_kind_mismatch"
    ):
        _materials_page(
            session,
            context,
            session.get(BuildDraft, identity),
            session.get(DraftPreparation, task),
        )
    # P2 已接通独立图片候选；继续准备不会再进入旧视频选材函数。
    continue_draft(session, context=context, task_id=task)
    with pytest.raises(
        DomainError, check=lambda e: e.code == "draft_media_kind_mismatch"
    ):
        edit_material_groups(
            session,
            context=context,
            draft_id=identity,
            drama_id=uuid4(),
            expected_revision=1,
            groups=[],
        )
    assert not session.exec(
        select(DraftGroupMaterial).where(DraftGroupMaterial.draft_id == identity)
    ).all()


def test_migrated_composition_tables_match_runtime_metadata(session):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    tables = {"build_draft", "draft_creative_unit", "draft_creative_unit_item"}

    def include(obj, _name, kind, _reflected, _compare_to):
        return (obj.name if kind == "table" else obj.table.name) in tables

    migration_context = MigrationContext.configure(
        session.connection(), opts={"compare_type": True, "include_object": include}
    )
    assert compare_metadata(migration_context, SQLModel.metadata) == []


def test_members_require_correct_media_type_but_allow_other_upload_bc(
    session, context, intent
):
    draft, drama = scaffold(session, context, intent)
    row = unit(draft, drama)
    session.add(row)
    session.flush()
    video = material(session, context, "video.mp4", bc="bc-draft")
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(member(row, video.id))
        session.flush()
    image = material(session, context, "elsewhere.jpg", bc="bc-other-upload")
    image.media_type = "IMAGE"
    session.flush()
    session.add(member(row, image.id))
    session.flush()
    assert (
        session.get(
            DraftCreativeUnitItem, (row.tenant_id, row.draft_id, 1, row.id, 1)
        ).material_id
        == image.id
    )
