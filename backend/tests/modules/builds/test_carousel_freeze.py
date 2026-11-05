"""真实 PostgreSQL 的不可变 V2 快照、版本读取及冻结门禁。"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.creative_units import (
    creative_content_digest,
    frozen_ad_creative_units,
    require_freeze_ready,
    snapshot_creative_units,
)
from app.modules.builds.image_composition_models import DraftCreativeUnit
from app.modules.builds.image_composition_schemas import ImageRule
from app.modules.builds.preview_models import (
    PreviewAdCreativeUnit,
    PreviewContract,
    PreviewCreativeUnit,
    PreviewCreativeUnitItem,
    PreviewDrama,
    PreviewDramaGroup,
)
from app.modules.materials.models import MaterialFile
from tests.modules.builds.test_draft_catalog import preview
from tests.modules.builds.test_image_composition_service import apply_rule
from tests.modules.builds.test_image_composition_service import (
    image_draft as image_draft,
)


def frozen_case(session, context, image_draft):
    draft, drama, scope = image_draft
    apply_rule(
        session,
        image_draft,
        ImageRule(music_mode="FIXED", fixed_music_id="song", single_music_id="single"),
    )
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation

    for run in session.exec(
        select(MusicPreparationRun).where(
            MusicPreparationRun.draft_id == draft.id,
            MusicPreparationRun.status == "PENDING",
        )
    ).all():
        advance_music_preparation(session, run.id, run.generation)
    require_freeze_ready(session, draft, context=context, route=scope.route)
    value = preview(
        session,
        context,
        draft.id,
        {"bc_id": draft.bc_id, "strategy_version_id": draft.strategy_version_id},
        revision=draft.revision,
    )
    shared = {
        "tenant_id": context.tenant_id,
        "preview_id": value.id,
        "bc_id": draft.bc_id,
    }
    session.add(PreviewContract(**shared, version=2, media_kind="CAROUSEL"))
    records = snapshot_creative_units(session, preview=value, draft=draft)
    session.add(
        PreviewDrama(
            **shared,
            drama_id=drama.drama_id,
            link_id=drama.link_id,
            title=drama.title,
            url="https://example.com",
            protected_base="test",
        )
    )
    session.flush()
    session.add(PreviewDramaGroup(**shared, drama_id=drama.drama_id, group_no=1))
    session.flush()
    units = session.exec(
        select(PreviewCreativeUnit)
        .where(PreviewCreativeUnit.preview_id == value.id)
        .order_by(PreviewCreativeUnit.position)
    ).all()
    for index, unit in enumerate(units, 1):
        session.add(
            PreviewAdCreativeUnit(
                **shared,
                drama_id=drama.drama_id,
                group_no=1,
                base_ad_no=1,
                position=index,
                creative_unit_id=unit.id,
            )
        )
    session.flush()
    return value, records


def test_music_and_order_are_part_of_content_identity():
    members = [(uuid4(), "a" * 64), (uuid4(), "b" * 64)]
    assert (
        len(
            {
                creative_content_digest("CAROUSEL", members, "a"),
                creative_content_digest("CAROUSEL", members, "b"),
                creative_content_digest("CAROUSEL", members[::-1], "a"),
            }
        )
        == 3
    )


def test_snapshot_survives_original_rename_and_draft_music_change(
    session, context, image_draft
):
    draft, drama, _ = image_draft
    value, records = frozen_case(session, context, image_draft)
    kwargs = {
        "tenant_id": context.tenant_id,
        "preview_id": value.id,
        "drama_id": drama.drama_id,
        "group_no": 1,
        "base_ad_no": 1,
    }
    before = frozen_ad_creative_units(session, **kwargs)
    assert [len(u.members) for u in before] == [3, 3, 1]
    value.content_digest = "f" * 64
    value.status = "FROZEN"
    session.add(value)
    session.flush()
    material = session.get(MaterialFile, before[0].members[0].material_id)
    material.file_name = "renamed.jpg"
    source = session.exec(
        select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == draft.id)
    ).first()
    source.music_id, source.music_revision = "replacement", source.music_revision + 1
    session.add(source)
    session.add(material)
    session.flush()
    assert frozen_ad_creative_units(session, **kwargs) == before
    assert value.content_digest == "f" * 64
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "UPDATE preview_creative_unit SET music_id=:song WHERE preview_id=:id"
            ),
            {"song": "bad", "id": value.id},
        )
    with pytest.raises(DomainError) as error:
        frozen_ad_creative_units(session, **(kwargs | {"base_ad_no": 2}))
    assert error.value.code == "empty_creative_mapping"


def test_freeze_rejects_unready_music_and_stale_rules(session, context, image_draft):
    draft, _, scope = image_draft
    apply_rule(session, image_draft)
    value = preview(
        session,
        context,
        draft.id,
        {"bc_id": draft.bc_id, "strategy_version_id": draft.strategy_version_id},
        revision=draft.revision,
    )
    with pytest.raises(DomainError) as error:
        snapshot_creative_units(session, preview=value, draft=draft)
    assert error.value.code == "music_not_ready"
    draft.applied_image_rule_revision = None
    with pytest.raises(DomainError) as error:
        require_freeze_ready(session, draft, context=context, route=scope.route)
    assert error.value.code == "image_rules_not_applied"


def test_v2_tables_enforce_tenant_and_parent_immutability(
    session, context, image_draft
):
    value, _ = frozen_case(session, context, image_draft)
    session.exec(
        select(PreviewCreativeUnitItem).where(
            PreviewCreativeUnitItem.preview_id == value.id
        )
    ).first()
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "UPDATE preview_creative_unit_item SET drama_id=:other WHERE preview_id=:preview"
            ),
            {"other": uuid4(), "preview": value.id},
        )
    value.status = "FROZEN"
    session.add(value)
    session.flush()
    for table in (
        "preview_contract",
        "preview_creative_unit_item",
        "preview_ad_creative_unit",
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(f"DELETE FROM {table} WHERE preview_id=:id"), {"id": value.id}
            )


def test_historical_migration_adds_layout_without_changing_frozen_bytes(monkeypatch):
    from alembic import command
    from sqlmodel import Session

    from app.modules.builds.preview_materials import frozen_ad_material_ids
    from app.modules.builds.preview_models import (
        BuildPreview,
        PreviewAdMaterial,
        PreviewGroupMaterial,
    )
    from tests.migration_database import historical_database
    from tests.modules.builds.test_drafts import create_intent
    from tests.modules.builds.test_image_composition_migration import scaffold
    from tests.modules.conftest import create_context
    from tests.modules.materials.test_tenant_materials import material

    with historical_database(monkeypatch, "0059_video_creative_units") as (
        engine,
        config,
    ):
        frozen = []
        with Session(engine) as db:
            context = create_context(db)
            intent = create_intent(db, context)
            draft, drama = scaffold(db, context, intent)
            for number, layout in enumerate(("GROUP", "AD"), 1):
                original = material(db, context, f"{layout}.mp4")
                value = preview(db, context, draft.id, intent, revision=number)
                shared = {
                    "tenant_id": context.tenant_id,
                    "preview_id": value.id,
                    "bc_id": draft.bc_id,
                }
                db.add(
                    PreviewDrama(
                        **shared,
                        drama_id=drama.drama_id,
                        link_id=drama.link_id,
                        title=drama.title,
                        url="https://example.com",
                        protected_base="frozen",
                    )
                )
                db.flush()
                db.add(PreviewDramaGroup(**shared, drama_id=drama.drama_id, group_no=1))
                db.flush()
                db.add(
                    PreviewGroupMaterial(
                        **shared,
                        drama_id=drama.drama_id,
                        group_no=1,
                        position=1,
                        material_id=original.id,
                    )
                )
                if layout == "AD":
                    db.add(
                        PreviewAdMaterial(
                            **shared,
                            drama_id=drama.drama_id,
                            group_no=1,
                            base_ad_no=1,
                            position=1,
                            material_id=original.id,
                        )
                    )
                db.flush()
                value.content_digest, value.status = "a" * 64, "FROZEN"
                db.add(value)
                db.flush()
                raw = db.execute(
                    text(
                        "SELECT row_to_json(p)::text FROM build_preview p WHERE id=:id"
                    ),
                    {"id": value.id},
                ).scalar_one()
                frozen.append((value.id, drama.drama_id, original.id, layout, raw))
            tenant_id = context.tenant_id
            db.commit()
        command.upgrade(config, "0060_frozen_creative_units")
        with Session(engine) as db:
            for identity, drama_id, material_id, layout, raw in frozen:
                assert (
                    db.execute(
                        text(
                            "SELECT row_to_json(p)::text FROM build_preview p WHERE id=:id"
                        ),
                        {"id": identity},
                    ).scalar_one()
                    == raw
                )
                descriptor = db.get(PreviewContract, (tenant_id, identity))
                assert (descriptor.version, descriptor.legacy_material_layout) == (
                    1,
                    layout,
                )
                assert frozen_ad_material_ids(
                    db,
                    tenant_id=tenant_id,
                    preview_id=identity,
                    drama_id=drama_id,
                    group_no=1,
                    base_ad_no=1,
                ) == [material_id]
                if layout == "AD":
                    assert (
                        frozen_ad_material_ids(
                            db,
                            tenant_id=tenant_id,
                            preview_id=identity,
                            drama_id=drama_id,
                            group_no=1,
                            base_ad_no=2,
                        )
                        == []
                    )
                with pytest.raises(DBAPIError), db.begin_nested():
                    db.execute(
                        text("DELETE FROM preview_contract WHERE preview_id=:id"),
                        {"id": identity},
                    )

            old = db.get(BuildPreview, frozen[0][0])
            building = preview(
                db,
                context,
                old.draft_id,
                {"bc_id": old.bc_id, "strategy_version_id": old.strategy_version_id},
                revision=3,
            )
            with pytest.raises(DBAPIError), db.begin_nested():
                db.add(
                    PreviewContract(
                        tenant_id=tenant_id,
                        preview_id=building.id,
                        bc_id=old.bc_id,
                        version=1,
                        media_kind="VIDEO",
                        legacy_material_layout=None,
                    )
                )
                db.flush()


def test_pending_music_scan_with_zero_jobs_blocks_freeze(session, context, image_draft):
    draft, _, scope = image_draft
    apply_rule(
        session,
        image_draft,
        ImageRule(music_mode="FIXED", fixed_music_id="song", single_music_id="single"),
    )
    from app.modules.builds.carousel_music_models import MusicSelectionJob

    assert not session.exec(
        select(MusicSelectionJob).where(MusicSelectionJob.draft_id == draft.id)
    ).all()
    with pytest.raises(DomainError) as error:
        require_freeze_ready(session, draft, context=context, route=scope.route)
    assert error.value.code == "music_preparation_pending"


def test_same_images_different_music_remain_independent_units(
    session, context, image_draft
):
    from uuid import UUID

    from app.modules.builds.image_composition_commands import Duplicate, SetMusic
    from tests.modules.builds.test_image_composition_service import edit

    draft, drama, _ = image_draft
    units = apply_rule(
        session,
        image_draft,
        ImageRule(music_mode="FIXED", fixed_music_id="song", single_music_id="single"),
    )
    copy = edit(
        session, image_draft, Duplicate(kind="DUPLICATE", unit_id=UUID(units[0]["id"]))
    )
    edit(
        session,
        image_draft,
        SetMusic(
            kind="SET_MUSIC", unit_id=UUID(copy["affected_units"][0]), music_id="other"
        ),
    )
    value = preview(
        session,
        context,
        draft.id,
        {"bc_id": draft.bc_id, "strategy_version_id": draft.strategy_version_id},
        revision=draft.revision,
    )
    snapshot_creative_units(session, preview=value, draft=draft)
    rows = session.exec(
        select(PreviewCreativeUnit)
        .where(PreviewCreativeUnit.preview_id == value.id)
        .order_by(PreviewCreativeUnit.position)
    ).all()
    assert len(rows) == 4
    assert rows[0].content_digest != rows[-1].content_digest
    assert rows[0].id != rows[-1].id and rows[-1].music_id == "other"


def test_changed_original_bytes_cannot_freeze_old_music_content(
    session, context, image_draft
):
    draft, _, _ = image_draft
    apply_rule(
        session,
        image_draft,
        ImageRule(music_mode="FIXED", fixed_music_id="song", single_music_id="single"),
    )
    material = session.exec(
        select(MaterialFile).where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.media_type == "IMAGE",
        )
    ).first()
    material.sha256 = "f" * 64
    session.add(material)
    session.flush()
    value = preview(
        session,
        context,
        draft.id,
        {"bc_id": draft.bc_id, "strategy_version_id": draft.strategy_version_id},
        revision=draft.revision,
    )
    with pytest.raises(DomainError) as error:
        snapshot_creative_units(session, preview=value, draft=draft)
    assert error.value.code == "image_content_changed"


def test_carousel_preview_uses_frozen_units_through_async_expansion(
    session, context, image_draft, monkeypatch
):
    from types import SimpleNamespace

    from app.modules.builds import identity_selection, mini_selection, previews
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.carousel_music_tasks import advance_music_preparation
    from app.modules.builds.preview_models import (
        BuildPreview,
        BuildUnit,
        PreviewAdMaterial,
        PreviewGroupMaterial,
    )
    from app.modules.builds.scene_schemas import SceneContext
    from tests.modules.builds.test_previews import drain

    draft, _, _ = image_draft
    apply_rule(
        session,
        image_draft,
        ImageRule(music_mode="RECOMMEND", single_music_id="single"),
    )
    for run in session.exec(
        select(MusicPreparationRun).where(MusicPreparationRun.draft_id == draft.id)
    ).all():
        advance_music_preparation(session, run.id, run.generation)
    from app.modules.builds.carousel_music import request_music
    from app.modules.builds.image_composition_commands import SetImages
    from app.modules.builds.image_composition_models import DraftCreativeUnitItem
    from app.modules.builds.image_composition_service import edit_creative_units

    sources = session.exec(
        select(DraftCreativeUnit)
        .where(DraftCreativeUnit.draft_id == draft.id)
        .order_by(DraftCreativeUnit.position)
    ).all()
    for source in sources:
        request_music(
            session,
            image_draft[2],
            source.id,
            source.revision,
            source.music_revision,
            uuid4(),
            "SET_MANUAL",
            "selected-song",
        )
    source = sources[0]
    source.music_snapshot = {
        **source.music_snapshot,
        "music_id": "selected-song",
        "display_name": "Selected title",
        "author": "Composer",
        "duration": 12.5,
        "preview_url": "https://temporary.invalid/private",
        "unexpected": {"url": "https://temporary.invalid/hidden"},
    }
    session.add(source)
    session.flush()
    request_music(
        session,
        image_draft[2],
        source.id,
        source.revision,
        source.music_revision,
        uuid4(),
        "RECOMMEND",
    )
    assert "pending_request" in source.music_snapshot
    ids = session.exec(
        select(DraftCreativeUnitItem.material_id)
        .where(DraftCreativeUnitItem.unit_id == source.id)
        .order_by(DraftCreativeUnitItem.position)
    ).all()
    edit_creative_units(
        session,
        image_draft[2],
        uuid4(),
        draft.revision,
        SetImages(
            kind="SET_IMAGES", unit_id=source.id, material_ids=tuple(reversed(ids))
        ),
    )
    source = session.get(
        DraftCreativeUnit,
        (context.tenant_id, draft.id, draft.active_image_generation, source.id),
    )
    assert source.music_status == "READY" and "pending_request" in source.music_snapshot
    for run in session.exec(
        select(MusicPreparationRun).where(
            MusicPreparationRun.draft_id == draft.id,
            MusicPreparationRun.status == "PENDING",
        )
    ).all():
        advance_music_preparation(session, run.id, run.generation)
    draft.status = "READY"
    session.add(draft)
    session.flush()
    monkeypatch.setattr(mini_selection, "require_preview_mini", lambda *a, **k: "mini")
    monkeypatch.setattr(
        identity_selection,
        "require_preview_identity",
        lambda *a, **k: {"identity_type": "TT_USER", "identity_id": "identity"},
    )
    monkeypatch.setattr(
        previews,
        "draft_directory",
        lambda *a, **k: SimpleNamespace(
            state="READY", unavailable_region_codes=[], region_codes=["US"]
        ),
    )
    monkeypatch.setattr(
        previews,
        "read_scene_context",
        lambda *a, **k: SceneContext(
            supported=False,
            reason_codes=("identity_unavailable",),
            media_kind="CAROUSEL",
            creative_limit=50,
            capability_revision="test",
        ),
    )
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **k: {
            i: SimpleNamespace(state="ready", reason_code=None)
            for i in k["material_ids"]
        },
    )
    identity = previews.generate_preview(
        session, context=context, draft_id=draft.id, expected_revision=draft.revision
    )
    stored = session.get(
        PreviewCreativeUnit, (context.tenant_id, identity, source.id)
    ).music_snapshot
    assert stored == {
        "music_id": "selected-song",
        "display_name": "Selected title",
        "author": "Composer",
        "duration": 12.5,
        "selection_source": "MANUAL",
        "images_changed": True,
    }
    frozen_music = [
        u.music_id
        for u in session.exec(
            select(PreviewCreativeUnit)
            .where(PreviewCreativeUnit.preview_id == identity)
            .order_by(PreviewCreativeUnit.position)
        ).all()
    ]
    # 后台进度不 bump draft.revision；异步冻结只能继续已捕获内容。
    for source in session.exec(
        select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == draft.id)
    ).all():
        source.music_id = "late-music"
        source.music_revision += 1
        session.add(source)
    session.flush()
    drain(session, context, identity)
    assert session.get(BuildPreview, identity).status == "FROZEN"
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.media_kind == "CAROUSEL" and summary.contract_version == 2
    assert summary.blocked_count == 1
    assert not session.exec(
        select(PreviewAdMaterial).where(PreviewAdMaterial.preview_id == identity)
    ).all()
    assert not session.exec(
        select(PreviewGroupMaterial).where(PreviewGroupMaterial.preview_id == identity)
    ).all()
    assert [
        u.music_id
        for u in session.exec(
            select(PreviewCreativeUnit)
            .where(PreviewCreativeUnit.preview_id == identity)
            .order_by(PreviewCreativeUnit.position)
        ).all()
    ] == frozen_music
    unit = session.exec(select(BuildUnit).where(BuildUnit.preview_id == identity)).one()
    assert "identity_unavailable" in unit.reason_codes
    groups = previews.get_frozen_groups(session, context=context, unit_id=unit.id).items
    from app.models import User
    from app.modules.builds.api import frozen_ads

    payload = frozen_ads(
        context.tenant_id,
        unit.id,
        groups[0].group_id,
        session,
        session.get(User, context.actor_id),
        cursor=None,
        limit=2,
    ).model_dump_json()
    assert (
        "pending_request" not in payload
        and "unexpected" not in payload
        and "temporary.invalid" not in payload
        and "preview_url" not in payload
    )
    assert (
        "Selected title" in payload
        and "MANUAL" in payload
        and "selected-song" in payload
    )
    assert groups and all(
        ad.creative_units
        for group in groups
        for ad in previews.get_frozen_ads(
            session, context=context, unit_id=unit.id, group_id=group.group_id
        ).items
    )


def test_set_based_capture_10000_images_has_constant_query_count(
    session, context, image_draft
):
    from time import monotonic

    from sqlalchemy import event, func

    from app.modules.builds.models import BuildDraft
    from app.modules.strategies.structure import estimate_batch_structure
    from tests.modules.builds.test_structure_preview import strategy

    draft, drama, scope = image_draft
    values = {
        "tenant": draft.tenant_id,
        "draft": draft.id,
        "bc": draft.bc_id,
        "drama": drama.drama_id,
    }
    session.execute(
        text(
            "CREATE TEMP TABLE scale_images ON COMMIT DROP AS SELECT gen_random_uuid() id,n FROM generate_series(1,10000) n"
        )
    )
    session.execute(
        text("""
      INSERT INTO material_file (id,tenant_id,bc_id,file_name,file_name_folded,object_key,byte_size,sha256,media_type,mime_type,storage_state,created_at)
      SELECT id,:tenant,:bc,'scale-'||n||'.jpg','scale-'||n||'.jpg','test/'||id,1000,
        encode(sha256(convert_to(n::text,'UTF8')),'hex'),'IMAGE','image/jpeg','stored',now() FROM scale_images
    """),
        values,
    )
    session.execute(
        text(
            "CREATE TEMP TABLE scale_units ON COMMIT DROP AS SELECT gen_random_uuid() id,n FROM generate_series(1,2000) n"
        )
    )
    session.execute(
        text("""
      INSERT INTO draft_creative_unit (tenant_id,draft_id,generation,id,bc_id,drama_id,kind,position,content_digest,revision,composition_protected,music_protected,music_id,music_rule_digest,music_status,music_revision,music_request_generation,music_snapshot)
      SELECT :tenant,:draft,1,id,:bc,:drama,'CAROUSEL',n,repeat('0',64),1,false,true,'scale-song','','READY',7,1,'{"source":"MANUAL"}'::jsonb FROM scale_units
    """),
        values,
    )
    session.execute(
        text("""
      INSERT INTO draft_creative_unit_item (tenant_id,draft_id,generation,unit_id,position,bc_id,drama_id,material_id,kind,media_type)
      SELECT :tenant,:draft,1,u.id,(i.n-1)%5+1,:bc,:drama,i.id,'CAROUSEL','IMAGE'
      FROM scale_images i JOIN scale_units u ON u.n=(i.n-1)/5+1
    """),
        values,
    )
    session.execute(
        text("""
      UPDATE draft_creative_unit u SET content_digest=s.digest FROM (
        SELECT i.unit_id,encode(sha256(convert_to(replace(json_agg(json_build_array(i.material_id::text,f.sha256) ORDER BY i.position)::text,', ',','),'UTF8')),'hex') AS digest
        FROM draft_creative_unit_item i JOIN material_file f ON f.id=i.material_id AND f.tenant_id=i.tenant_id
        WHERE i.tenant_id=:tenant AND i.draft_id=:draft GROUP BY i.unit_id
      ) s WHERE u.id=s.unit_id AND u.tenant_id=:tenant AND u.draft_id=:draft
    """),
        values,
    )
    session.execute(
        text("""
      INSERT INTO image_candidate_exclusion (tenant_id,draft_id,drama_id,material_id,bc_id,excluded,cross_drama_allowed)
      SELECT tenant_id,draft_id,drama_id,material_id,bc_id,true,false FROM image_candidate_item WHERE tenant_id=:tenant AND draft_id=:draft
    """),
        values,
    )
    session.execute(
        text("""
      INSERT INTO image_candidate_item (tenant_id,snapshot_id,drama_id,material_id,draft_id,bc_id,media_type,candidate,source)
      SELECT :tenant,s.snapshot_id,:drama,f.id,:draft,:bc,'IMAGE',jsonb_build_object('file_name_snapshot',f.file_name,'content_digest',f.sha256),'{}'::jsonb
      FROM scale_images i JOIN material_file f ON f.id=i.id JOIN image_composition_state s ON s.tenant_id=:tenant AND s.draft_id=:draft
    """),
        values,
    )
    draft.active_image_generation = 1
    draft.applied_image_rule_revision = draft.revision
    session.add(draft)
    session.flush()
    estimate = estimate_batch_structure(
        [2000],
        account_count=1,
        config=strategy(
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="BY_MATERIAL",
            ads_per_group=None,
            ad_material_allocation=None,
            max_materials_per_ad=1,
            creative_count=1,
        ),
    )
    assert estimate.ad_count == 2000
    value = preview(
        session,
        context,
        draft.id,
        {"bc_id": draft.bc_id, "strategy_version_id": draft.strategy_version_id},
        revision=draft.revision,
    )
    session.add(
        PreviewContract(
            tenant_id=context.tenant_id,
            preview_id=value.id,
            bc_id=draft.bc_id,
            version=2,
            media_kind="CAROUSEL",
        )
    )
    session.flush()
    # 与实际入口相同：父草稿锁涵盖验证及音乐/内容拍照，记录全部 SQL 次数。
    session.exec(
        select(BuildDraft).where(BuildDraft.id == draft.id).with_for_update()
    ).one()
    from app.modules.builds.creative_units import SOURCE_CTE

    plan = session.execute(
        text("EXPLAIN (FORMAT JSON) " + SOURCE_CTE + "SELECT * FROM content"),
        values | {"generation": 1},
    ).scalar_one()
    nodes = []

    def collect(node):
        if "Relation Name" in node:
            nodes.append(
                (node["Node Type"], node["Relation Name"], node.get("Index Name"))
            )
        for child in node.get("Plans", []):
            collect(child)

    collect(plan[0]["Plan"])
    assert any(
        kind in {"Index Scan", "Index Only Scan"} and relation == "material_file"
        for kind, relation, _ in nodes
    )
    print("Capture source lookup plan:", nodes)  # noqa: T201 — local scale evidence
    queries, timings = [], []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)
        _context._capture_started = monotonic()

    def finished(_conn, _cursor, statement, _parameters, _context, _executemany):
        label = next(
            (
                line.strip()
                for line in statement.splitlines()
                if "INSERT INTO" in line
                or "SELECT CASE" in line
                or "SELECT count(*) FROM lock" in line
            ),
            statement.strip().splitlines()[0],
        )
        timings.append((label[:70], round(monotonic() - _context._capture_started, 3)))

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", observe)
    event.listen(connection, "after_cursor_execute", finished)
    started = monotonic()
    try:
        from app.modules.builds.preview_capture_limits import freeze_capture_limits

        with freeze_capture_limits(session):
            require_freeze_ready(session, draft, context=context, route=scope.route)
            hashes = snapshot_creative_units(session, preview=value, draft=draft)
    finally:
        elapsed = monotonic() - started
        event.remove(connection, "before_cursor_execute", observe)
        event.remove(connection, "after_cursor_execute", finished)
    assert len(queries) <= 24
    assert len(hashes) == 3 and all(len(h) == 64 for h in hashes.values())
    assert (
        session.exec(
            select(func.count())
            .select_from(PreviewCreativeUnit)
            .where(PreviewCreativeUnit.preview_id == value.id)
        ).one()
        == 2000
    )
    assert (
        session.exec(
            select(func.count())
            .select_from(PreviewCreativeUnitItem)
            .where(PreviewCreativeUnitItem.preview_id == value.id)
        ).one()
        == 10000
    )
    first = session.exec(
        select(PreviewCreativeUnit)
        .where(PreviewCreativeUnit.preview_id == value.id)
        .order_by(PreviewCreativeUnit.position)
        .limit(1)
    ).one()
    members = session.exec(
        select(PreviewCreativeUnitItem)
        .where(
            PreviewCreativeUnitItem.preview_id == value.id,
            PreviewCreativeUnitItem.creative_unit_id == first.id,
        )
        .order_by(PreviewCreativeUnitItem.position)
    ).all()
    assert [m.position for m in members] == [1, 2, 3, 4, 5]
    assert first.music_revision == 7 and first.music_id == "scale-song"
    assert first.content_digest == creative_content_digest(
        "CAROUSEL", [(m.material_id, m.content_digest) for m in members], "scale-song"
    )
    # 同批大分组读数不能受旧100成员/100广告限制；分页广告仍返回完整成员。
    from app.modules.builds import previews
    from app.modules.builds.models import DraftAccount
    from app.modules.builds.preview_models import BuildUnit, PlannedGroup
    from app.modules.strategies.models import CopyEntry

    session.add(
        PreviewDrama(
            tenant_id=context.tenant_id,
            preview_id=value.id,
            bc_id=draft.bc_id,
            drama_id=drama.drama_id,
            link_id=drama.link_id,
            title=drama.title,
            url="https://example.com",
            protected_base="scale",
        )
    )
    session.flush()
    session.add(
        PreviewDramaGroup(
            tenant_id=context.tenant_id,
            preview_id=value.id,
            bc_id=draft.bc_id,
            drama_id=drama.drama_id,
            group_no=1,
        )
    )
    account = session.exec(
        select(DraftAccount).where(DraftAccount.draft_id == draft.id)
    ).first()
    account_unit = BuildUnit(
        tenant_id=context.tenant_id,
        preview_id=value.id,
        bc_id=draft.bc_id,
        drama_id=drama.drama_id,
        advertiser_id=account.advertiser_id,
        connection_id=scope.route.connection_id,
        currency="USD",
        timezone="UTC",
        campaign_name="scale",
        campaign_digest="a" * 64,
        scene_snapshot={},
        complete=True,
        group_count=1,
        ad_count=2000,
    )
    session.add(account_unit)
    session.flush()
    group = PlannedGroup(
        tenant_id=context.tenant_id,
        preview_id=value.id,
        bc_id=draft.bc_id,
        unit_id=account_unit.id,
        drama_id=drama.drama_id,
        group_no=1,
        name="scale",
    )
    session.add(group)
    session.flush()
    copy = session.exec(select(CopyEntry)).first()
    plans = values | {"preview": value.id, "group": group.id, "copy": copy.id}
    session.execute(
        text("""
      INSERT INTO preview_group_creative_unit (tenant_id,preview_id,bc_id,drama_id,group_no,position,creative_unit_id)
      SELECT tenant_id,preview_id,bc_id,drama_id,1,position,id FROM preview_creative_unit WHERE tenant_id=:tenant AND preview_id=:preview
    """),
        plans,
    )
    session.execute(
        text("""
      INSERT INTO preview_ad_creative_unit (tenant_id,preview_id,bc_id,drama_id,group_no,base_ad_no,position,creative_unit_id)
      SELECT tenant_id,preview_id,bc_id,drama_id,1,position,1,id FROM preview_creative_unit WHERE tenant_id=:tenant AND preview_id=:preview
    """),
        plans,
    )
    session.execute(
        text("""
      INSERT INTO planned_ad (tenant_id,preview_id,bc_id,id,group_id,base_ad_no,creative_no,name,copy_id,text,cta_option_ids)
      SELECT tenant_id,preview_id,bc_id,gen_random_uuid(),:group,position,1,'Ad '||position,:copy,'Copy','[]'::jsonb
      FROM preview_creative_unit WHERE tenant_id=:tenant AND preview_id=:preview
    """),
        plans,
    )
    value.status, value.content_digest = "FROZEN", "f" * 64
    session.add(value)
    session.flush()
    summary = previews.get_frozen_groups(
        session, context=context, unit_id=account_unit.id
    ).items[0]
    assert (summary.material_count, summary.creative_unit_count, summary.ad_count) == (
        10000,
        2000,
        2000,
    )
    assert (
        summary.material_ids is None
        and summary.ads is None
        and len(summary.sample_material_ids) == 6
    )
    page = previews.get_frozen_ads(
        session, context=context, unit_id=account_unit.id, group_id=group.id, limit=2
    )
    assert page.total == 2000 and len(page.items) == 2
    assert [len(ad.creative_units[0].members) for ad in page.items] == [5, 5]
    print("Capture statement timings:", timings)  # noqa: T201 — local scale evidence
    print(  # noqa: T201 — local scale evidence
        f"10000 images / 2000 units: {len(queries)} capture queries, {elapsed:.3f}s local PostgreSQL"
    )


def test_v2_group_reads_over_100_ads_use_scoped_complete_pages(
    session, context, image_draft
):
    from app.modules.builds import previews
    from app.modules.builds.models import DraftAccount
    from app.modules.builds.preview_models import (
        BuildUnit,
        PlannedAd,
        PlannedGroup,
        PreviewGroupCreativeUnit,
    )
    from app.modules.strategies.models import CopyEntry

    draft, drama, scope = image_draft
    value, _ = frozen_case(session, context, image_draft)
    account = session.exec(
        select(DraftAccount).where(DraftAccount.draft_id == draft.id)
    ).first()
    unit = BuildUnit(
        tenant_id=context.tenant_id,
        preview_id=value.id,
        bc_id=draft.bc_id,
        drama_id=drama.drama_id,
        advertiser_id=account.advertiser_id,
        connection_id=scope.route.connection_id,
        currency="USD",
        timezone="UTC",
        campaign_name="test",
        campaign_digest="a" * 64,
        scene_snapshot={},
        complete=True,
        group_count=1,
        ad_count=101,
    )
    session.add(unit)
    session.flush()
    group = PlannedGroup(
        tenant_id=context.tenant_id,
        preview_id=value.id,
        bc_id=draft.bc_id,
        unit_id=unit.id,
        drama_id=drama.drama_id,
        group_no=1,
        name="101 ads",
    )
    session.add(group)
    session.flush()
    creatives = session.exec(
        select(PreviewCreativeUnit)
        .where(PreviewCreativeUnit.preview_id == value.id)
        .order_by(PreviewCreativeUnit.position)
    ).all()
    for c in creatives:
        session.add(
            PreviewGroupCreativeUnit(
                tenant_id=context.tenant_id,
                preview_id=value.id,
                bc_id=draft.bc_id,
                drama_id=drama.drama_id,
                group_no=1,
                position=c.position,
                creative_unit_id=c.id,
            )
        )
    copy = session.exec(select(CopyEntry)).first()
    for number in range(1, 102):
        if number > 1:
            session.add(
                PreviewAdCreativeUnit(
                    tenant_id=context.tenant_id,
                    preview_id=value.id,
                    bc_id=draft.bc_id,
                    drama_id=drama.drama_id,
                    group_no=1,
                    base_ad_no=number,
                    position=1,
                    creative_unit_id=creatives[0].id,
                )
            )
        session.add(
            PlannedAd(
                tenant_id=context.tenant_id,
                preview_id=value.id,
                bc_id=draft.bc_id,
                group_id=group.id,
                base_ad_no=number,
                creative_no=1,
                name=f"Ad {number}",
                copy_id=copy.id,
                text=copy.text,
            )
        )
    session.flush()
    value.status, value.content_digest = "FROZEN", "f" * 64
    session.add(value)
    session.flush()
    summary = previews.get_frozen_groups(
        session, context=context, unit_id=unit.id
    ).items[0]
    assert (summary.material_count, summary.creative_unit_count, summary.ad_count) == (
        7,
        3,
        101,
    )
    assert (
        summary.ads is None
        and summary.material_ids is None
        and len(summary.sample_material_ids) == 6
    )
    first = previews.get_frozen_ads(
        session, context=context, unit_id=unit.id, group_id=group.id, limit=2
    )
    assert first.total == 101 and [a.base_ad_no for a in first.items] == [1, 2]
    assert [len(c.members) for c in first.items[0].creative_units] == [3, 3, 1]
    second = previews.get_frozen_ads(
        session,
        context=context,
        unit_id=unit.id,
        group_id=group.id,
        limit=2,
        cursor=first.next_cursor,
    )
    assert [a.base_ad_no for a in second.items] == [3, 4]
    with pytest.raises(DomainError):
        previews.get_frozen_ads(
            session,
            context=context,
            unit_id=unit.id,
            group_id=uuid4(),
            cursor=first.next_cursor,
        )
    with pytest.raises(DomainError):
        previews.get_frozen_ads(
            session,
            context=context,
            unit_id=unit.id,
            group_id=group.id,
            cursor=first.next_cursor + "bad",
        )
    with pytest.raises(DomainError):
        previews.get_frozen_ads(
            session, context=context, unit_id=unit.id, group_id=group.id, limit=6
        )


def test_capture_query_timeout_rolls_back_snapshot_and_restores_settings(
    session, context, image_draft, monkeypatch
):
    from app.modules.builds import preview_capture_limits
    from app.modules.builds.preview_models import BuildPreview

    draft, _, _ = image_draft
    before = session.execute(
        text(
            "SELECT current_setting('statement_timeout'),current_setting('lock_timeout')"
        )
    ).one()
    monkeypatch.setattr(preview_capture_limits, "QUERY_TIMEOUT_MS", 10)
    with pytest.raises(DomainError) as error:
        with preview_capture_limits.freeze_capture_limits(session):
            row = preview(
                session,
                context,
                draft.id,
                {
                    "bc_id": draft.bc_id,
                    "strategy_version_id": draft.strategy_version_id,
                },
                revision=draft.revision,
            )
            identity = row.id
            session.execute(text("SELECT pg_sleep(0.1)"))
    assert error.value.code == "preview_freeze_busy"
    assert session.get(BuildPreview, identity) is None
    assert (
        session.execute(
            text(
                "SELECT current_setting('statement_timeout'),current_setting('lock_timeout')"
            )
        ).one()
        == before
    )


def test_only_current_pending_candidate_refresh_blocks_freeze(
    session, context, image_draft
):
    from app.modules.builds.image_composition_state import ImageCandidateSnapshot

    draft, _, scope = image_draft
    frozen_case(session, context, image_draft)
    pending = ImageCandidateSnapshot(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        bc_id=draft.bc_id,
        base_revision=draft.revision - 1,
        status="PENDING",
        actor_id=context.actor_id,
        route=scope.route.model_dump(mode="json"),
    )
    session.add(pending)
    session.flush()
    require_freeze_ready(session, draft, context=context, route=scope.route)
    pending.base_revision = draft.revision
    session.add(pending)
    session.flush()
    with pytest.raises(DomainError) as error:
        require_freeze_ready(session, draft, context=context, route=scope.route)
    assert error.value.code == "image_composition_pending"


def test_material_projection_requires_explicit_v2_descriptor(
    session, context, image_draft
):
    from app.modules.builds.preview_material_views import AdMaterialView

    value, _ = frozen_case(session, context, image_draft)
    query = select(AdMaterialView.material_id).where(
        AdMaterialView.preview_id == value.id
    )
    assert session.exec(query).all()
    descriptor = session.get(PreviewContract, (context.tenant_id, value.id))
    descriptor.version, descriptor.media_kind, descriptor.legacy_material_layout = (
        1,
        "VIDEO",
        "UNKNOWN",
    )
    session.add(descriptor)
    session.flush()
    assert session.exec(query).all() == []


def test_freeze_ignores_obsolete_music_subscribers_and_retry_intent(
    session, context, image_draft
):
    from app.modules.builds.carousel_music import _target
    from app.modules.builds.carousel_music_models import (
        MusicPreparationRun,
        MusicSelectionJob,
    )

    draft, drama, scope = image_draft
    frozen_case(session, context, image_draft)
    unit = session.exec(
        select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == draft.id)
    ).first()
    unit.music_protected = False
    session.add(unit)
    job = MusicSelectionJob(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        bc_id=draft.bc_id,
        actor_id=context.actor_id,
        active_generation=draft.active_image_generation,
        route=scope.route.model_dump(mode="json"),
        fingerprint="e" * 64,
        parameters={},
        material_ids=[],
        targets=[_target(unit)],
    )
    session.add(job)
    session.flush()
    with pytest.raises(DomainError) as error:
        require_freeze_ready(session, draft, context=context, route=scope.route)
    assert error.value.code == "music_preparation_pending"
    unit.music_protected = True
    unit.music_request_generation += 1
    session.add(unit)
    retry = MusicPreparationRun(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        bc_id=draft.bc_id,
        actor_id=context.actor_id,
        active_generation=draft.active_image_generation,
        route=scope.route.model_dump(mode="json"),
        action="RETRY_FAILED",
        base_revision=draft.revision - 1,
        drama_ids=[str(drama.drama_id)],
    )
    session.add(retry)
    session.flush()
    require_freeze_ready(session, draft, context=context, route=scope.route)
    retry.base_revision = draft.revision
    session.add(retry)
    session.flush()
    with pytest.raises(DomainError) as error:
        require_freeze_ready(session, draft, context=context, route=scope.route)
    assert error.value.code == "music_preparation_pending"
    from app.modules.builds.previews import _bump_preview_retry_revision

    with pytest.raises(DomainError):
        _bump_preview_retry_revision(session, draft, draft.revision)
    retry.base_revision = draft.revision - 1
    session.add(retry)
    session.flush()
    previous_revision = draft.revision
    _bump_preview_retry_revision(session, draft, previous_revision)
    assert draft.revision == previous_revision + 1
    assert draft.applied_image_rule_revision == draft.revision
