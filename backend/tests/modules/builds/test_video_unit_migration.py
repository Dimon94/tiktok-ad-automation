"""真实历史 PostgreSQL 升级保持旧分组顺序、请求身份和冻结正文。"""

import pytest
from alembic import command
from sqlalchemy import MetaData, Table
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.accounts.resolver import encode_cursor
from app.modules.builds.catalog import materials_page
from app.modules.builds.drafts import (
    edit_material_groups,
)
from app.modules.builds.image_composition_models import (
    DraftCreativeUnit,
    DraftCreativeUnitItem,
)
from app.modules.builds.models import (
    BuildDraft,
    DraftGroupMaterial,
)
from app.modules.builds.preview_models import BuildPreview
from app.modules.builds.video_units import (
    ordered_units,
    video_content_digest,
    video_material_ids,
)
from tests.migration_database import historical_database
from tests.modules.builds.test_draft_catalog import preview
from tests.modules.builds.test_drafts import create_intent
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import material


def test_video_backfill_preserves_order_request_and_frozen_bytes(monkeypatch):
    with historical_database(monkeypatch, "0058_selected_music_retry") as (
        engine,
        config,
    ):
        with Session(engine) as db:
            context = create_context(db)
            intent = create_intent(db, context, historical=True)
            from tests.modules.builds.test_image_composition_migration import (
                historical_scaffold,
            )

            draft, drama = historical_scaffold(db, context, intent, media_kind="VIDEO")
            draft_id = draft.id
            files = [
                material(db, context, name, bc="bc-draft")
                for name in ("Z.mp4", "A.mp4", "M.mp4")
            ]
            files[0].sha256 = None
            files[1].sha256 = "b" * 64
            digests = {
                file.id: video_content_digest(file.id, file.sha256) for file in files
            }
            ids = [file.id for file in files]
            for (group, position), identity in zip(
                ((1, 1), (1, 2), (4, 1)), ids, strict=True
            ):
                db.add(
                    DraftGroupMaterial(
                        tenant_id=context.tenant_id,
                        draft_id=draft_id,
                        bc_id=draft.bc_id,
                        drama_id=drama.drama_id,
                        group_no=group,
                        position=position,
                        material_id=identity,
                    )
                )
            draft.status = "READY"
            drama.material_state = "manual"
            drama.matched_count = 3
            frozen = preview(db, context, draft_id, intent, status="FROZEN")
            frozen_id, request, drama_id = frozen.id, draft.request_id, drama.drama_id
            db.refresh(frozen)
            frozen_before = frozen.model_dump(mode="json")
            db.commit()
        command.upgrade(config, "0059_video_creative_units")
        with Session(engine) as db:
            table = Table("build_draft", MetaData(), autoload_with=db.connection())
            historical = db.execute(table.select().where(table.c.id == draft_id)).one()
            assert historical.request_id == request and historical.media_kind == "VIDEO"
            assert (
                db.get(BuildPreview, frozen_id).model_dump(mode="json") == frozen_before
            )
            members = video_material_ids(db, historical, drama_id=drama_id)
            units = ordered_units(db, historical, drama_id=drama_id)
            assert [members[unit.id] for unit in units] == ids
            assert [unit.position for unit in units] == [1, 2, 3]
            assert {members[u.id]: u.content_digest for u in units} == digests
            assert all(
                unit.id not in ids and unit.music_status == "NOT_REQUIRED"
                for unit in units
            )
            assert not db.exec(
                select(DraftGroupMaterial).where(
                    DraftGroupMaterial.draft_id == draft_id
                )
            ).all()
        # 当期迁移的数据断言完成后，升级到 head 再验证今日业务服务。
        command.upgrade(config, "head")
        with Session(engine) as db:
            draft = db.get(BuildDraft, draft_id)
            assert draft.request_id == request
            assert (
                db.get(BuildPreview, frozen_id).model_dump(mode="json") == frozen_before
            )
            units = ordered_units(db, draft, drama_id=drama_id)
            members = video_material_ids(db, draft, drama_id=drama_id)
            assert [members[unit.id] for unit in units] == ids
            assert [unit.position for unit in units] == [1, 2, 3]
            assert {members[u.id]: u.content_digest for u in units} == digests
            assert all(
                unit.id not in ids and unit.music_status == "NOT_REQUIRED"
                for unit in units
            )
            assert not db.exec(
                select(DraftGroupMaterial).where(
                    DraftGroupMaterial.draft_id == draft_id
                )
            ).all()
            page = materials_page(
                db, context=context, draft_id=draft_id, drama_id=drama_id, limit=2
            )
            assert [row.material_id for row in page.items] == ids[:2]
            assert [row.group_no for row in page.items] == [1, 1]
            old_cursor = encode_cursor(
                scope={
                    "tenant": str(context.tenant_id),
                    "draft": str(draft_id),
                    "revision": str(draft.revision),
                    "drama": str(drama_id),
                    "view": "materials",
                },
                last_id="4:1",
            )
            with pytest.raises(
                DomainError, check=lambda error: error.code == "invalid_cursor"
            ):
                materials_page(
                    db,
                    context=context,
                    draft_id=draft_id,
                    drama_id=drama_id,
                    cursor=old_cursor,
                )

            assert (
                materials_page(
                    db,
                    context=context,
                    draft_id=draft_id,
                    drama_id=drama_id,
                    cursor=page.next_cursor,
                )
                .items[0]
                .material_id
                == ids[2]
            )
            edit_material_groups(
                db,
                context=context,
                draft_id=draft_id,
                drama_id=drama_id,
                expected_revision=1,
                groups=[[ids[2]], [ids[0], ids[1]]],
            )
            assert [
                video_material_ids(db, draft, drama_id=drama_id)[u.id]
                for u in ordered_units(db, draft, drama_id=drama_id)
            ] == [ids[2], ids[0], ids[1]]
            assert db.get(BuildDraft, draft_id).request_id == request
            members = video_material_ids(db, draft, drama_id=drama_id)
            assert {
                members[u.id]: u.content_digest
                for u in ordered_units(db, draft, drama_id=drama_id)
            } == digests
            assert db.exec(
                select(DraftCreativeUnitItem).where(
                    DraftCreativeUnitItem.draft_id == draft_id
                )
            ).all()
            assert db.exec(
                select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == draft_id)
            ).all()
