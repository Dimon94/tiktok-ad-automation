"""定向选择不被丢弃，平台回读只比较实际观察字段。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.strategies.schemas import StrategyConfig
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.strategies.test_versions import config


def chosen(**updates):
    return {
        "region_mode": "SELECTED",
        "region_codes": ["US", "CA"],
        "languages": ["en"],
        "age_groups": ["AGE_25_34", "AGE_35_44"],
        "gender": "GENDER_FEMALE",
        **updates,
    }


def test_strategy_preserves_and_normalizes_targeting():
    value = StrategyConfig.model_validate(
        {**config().model_dump(), "targeting": chosen(region_codes=["US", "CA", "US"])}
    )
    assert value.model_dump(mode="json")["targeting"] == chosen(
        region_codes=["CA", "US"]
    )


def test_existing_strategy_has_explicit_tool_unrestricted_defaults():
    assert config().model_dump(mode="json")["targeting"] == {
        "region_mode": "ALL_AVAILABLE",
        "region_codes": [],
        "languages": [],
        "age_groups": [],
        "gender": "GENDER_UNLIMITED",
    }


@pytest.mark.parametrize(
    "values",
    [
        chosen(region_codes=[]),
        chosen(region_mode="ALL_AVAILABLE"),
        chosen(age_groups=["AGE_13_17"]),
        chosen(languages=["xx"]),
        chosen(gender="ANY"),
        chosen(region_codes=["usa"]),
        chosen(expand=True),
    ],
)
def test_invalid_selection_is_rejected(values):
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate({**config().model_dump(), "targeting": values})


def group_body():
    return {
        "advertiser_id": "100",
        "campaign_id": "200",
        "adgroup_name": "test",
        "minis_id": "mini",
        "roas_bid": "1",
        "schedule_start_time": "2026-09-28 00:00:00",
        "targeting_spec": {
            "location_ids": ["2", "1"],
            "languages": ["en"],
            "age_groups": ["AGE_35_44", "AGE_25_34"],
            "gender": "GENDER_FEMALE",
        },
        "targeting_optimization_mode": "MANUAL",
    }


def test_both_channels_preserve_strict_targeting_and_canonical_sets():
    from app.modules.builds.request_compiler import create_arguments, decode_intent

    intent = decode_intent("ADGROUP", group_body())
    for channel in ("OFFICIAL_API", "OFFICIAL_MCP"):
        _, body = create_arguments(attempt_id=uuid4(), intent=intent, channel=channel)
        assert body["targeting_optimization_mode"] == "MANUAL"
        assert body["targeting_spec"] == {
            "location_ids": ["1", "2"],
            "languages": ["en"],
            "age_groups": ["AGE_25_34", "AGE_35_44"],
            "gender": "GENDER_FEMALE",
        }
        assert "suggestion_audience_enabled" not in body


def test_custom_fields_cannot_silently_use_automatic_default():
    from app.modules.builds.request_compiler import decode_intent

    body = group_body()
    body.pop("targeting_optimization_mode")
    with pytest.raises(ValidationError):
        decode_intent("ADGROUP", body)


def test_readback_detects_missing_and_changed_targeting_but_ignores_order():
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
    )
    from app.modules.builds.readback_compare import parse_page
    from app.modules.builds.request_compiler import decode_intent, encode_intent

    intent = decode_intent("ADGROUP", group_body())
    row = {**encode_intent(intent), "adgroup_id": "300"}
    row["targeting_spec"]["age_groups"].reverse()
    query = BuildReadQuery(intent=intent, remote_id="300")

    def read():
        response = McpBusinessResponse(
            data={
                "list": [row],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            },
            evidence=CallEvidence(),
        )
        return parse_page(query=query, response=response).rows[0]

    assert read().intent == intent
    row["targeting_spec"]["gender"] = "GENDER_MALE"
    assert read().intent != intent
    del row["targeting_spec"]["languages"]
    assert read().intent is None


def test_targeting_resolves_only_the_common_country_ids_and_blocks_unavailable():
    from app.modules.builds import targeting
    from app.modules.builds.scene_schemas import SceneContext
    from app.modules.builds.targeting_schemas import AudienceTargeting

    scene = SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="verified",
        field_constraints={
            "target_regions": [
                {"region_code": "US", "location_id": "1"},
                {"region_code": "CA", "location_id": "2"},
            ]
        },
    )
    result = targeting.apply_targeting(scene, AudienceTargeting(), ["US"])
    assert result.adgroup_fields["targeting_spec"]["location_ids"] == ("1",)
    assert "targeting_optimization_mode" not in result.adgroup_fields
    assert "gender" not in result.adgroup_fields["targeting_spec"]
    restricted = targeting.apply_targeting(
        scene,
        AudienceTargeting(gender="GENDER_FEMALE"),
        ["US"],
    )
    assert restricted.adgroup_fields["targeting_optimization_mode"] == "MANUAL"
    assert restricted.adgroup_fields["targeting_spec"]["gender"] == "GENDER_FEMALE"
    bad = targeting.apply_targeting(
        scene, AudienceTargeting(**chosen(region_codes=["GB"])), ["US"]
    )
    assert not bad.supported
    assert "targeting_regions_unavailable" in bad.reason_codes

    minis_automatic_only = targeting.apply_targeting(
        replace(
            scene,
            field_constraints={
                **scene.field_constraints,
                "targeting_optimization_modes": ("AUTOMATIC",),
            },
        ),
        AudienceTargeting(gender="GENDER_FEMALE"),
        ["US"],
    )
    # 旧快照中的自动模式标记不再阻断用户明确选择的手动定向。
    assert minis_automatic_only.supported
    assert "targeting_manual_unverified" not in minis_automatic_only.reason_codes
    assert minis_automatic_only.adgroup_fields["targeting_optimization_mode"] == "MANUAL"
    assert minis_automatic_only.adgroup_fields["targeting_spec"]["gender"] == "GENDER_FEMALE"


def test_new_preview_freezes_effective_targeting(session, context, prepared):
    from sqlmodel import select

    from app.modules.builds import previews
    from app.modules.builds.models import BuildDraft
    from app.modules.builds.preview_models import BuildPreview, BuildUnit

    draft = session.get(BuildDraft, prepared)
    draft.targeting_override = chosen(region_codes=["US"])
    session.add(draft)
    session.flush()
    preview_id = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=draft.revision
    )
    from tests.modules.builds.test_previews import drain

    drain(session, context, preview_id)
    preview = session.get(BuildPreview, preview_id)
    assert preview.config["targeting"] == chosen(region_codes=["US"])
    units = session.exec(
        select(BuildUnit).where(BuildUnit.preview_id == preview_id)
    ).all()
    assert units
    assert all(
        u.scene_snapshot["adgroup_fields"]["targeting_spec"]["gender"]
        == "GENDER_FEMALE"
        for u in units
    )
    assert all(
        u.scene_snapshot["field_constraints"]["selected_region_codes"] == ["US"]
        for u in units
    )


def test_targeting_save_preserves_manual_groups_and_obsoletes_preview(
    session, context, prepared
):
    from sqlmodel import select

    from app.modules.builds import previews
    from app.modules.builds.image_composition_models import DraftCreativeUnit
    from app.modules.builds.models import BuildDraft, DraftDrama
    from app.modules.builds.preview_models import BuildPreview
    from app.modules.builds.targeting_schemas import TargetingChange
    from app.modules.builds.targeting_service import save_targeting
    from tests.modules.builds.test_previews import drain

    draft = session.get(BuildDraft, prepared)
    dramas = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).all()
    for drama in dramas:
        drama.material_state = "manual"
        session.add(drama)
    before = [
        r.model_dump()
        for r in session.exec(
            select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == prepared)
        ).all()
    ]
    assert before
    preview_id = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=draft.revision
    )
    drain(session, context, preview_id)
    frozen = session.get(BuildPreview, preview_id).config.copy()
    save_targeting(
        session,
        context=context,
        draft_id=prepared,
        body=TargetingChange(
            request_id=uuid4(),
            expected_revision=draft.revision,
            targeting_override=chosen(region_codes=["US"]),
        ),
    )
    after = [
        r.model_dump()
        for r in session.exec(
            select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == prepared)
        ).all()
    ]
    assert before == after
    assert all(d.material_state == "manual" for d in dramas)
    preview = session.get(BuildPreview, preview_id)
    session.refresh(preview)
    assert preview.status == "OBSOLETE"
    assert preview.config == frozen


def test_invalid_country_blocks_preview_instead_of_silently_narrowing(
    session, context, prepared
):
    from app.core.errors import DomainError
    from app.modules.builds import previews
    from app.modules.builds.models import BuildDraft

    draft = session.get(BuildDraft, prepared)
    draft.targeting_override = chosen(region_codes=["US", "GB"])
    session.add(draft)
    session.flush()
    with pytest.raises(DomainError) as error:
        previews.generate_preview(
            session,
            context=context,
            draft_id=prepared,
            expected_revision=draft.revision,
        )
    assert error.value.code == "targeting_regions_unavailable"


def test_build_arguments_reject_unrecognized_channel_before_send():
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.modules.builds.request_compiler import create_arguments, decode_intent

    with pytest.raises(RemoteCallError) as caught:
        create_arguments(
            attempt_id=uuid4(),
            intent=decode_intent("ADGROUP", group_body()),
            channel="unknown",
        )
    assert caught.value.code == "invalid_build_request"
    assert caught.value.effect == "NOT_SENT"


def test_targeting_directory_does_not_borrow_video_scene_for_carousel(
    session, context, prepared
):
    from app.modules.accounts.routing import freeze_route
    from app.modules.builds.models import BuildDraft
    from app.modules.builds.targeting_directory import region_directory

    draft = session.get(BuildDraft, prepared)
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    video = region_directory(
        session, context=context, route=route, draft_id=draft.id, media_kind="VIDEO"
    )
    carousel = region_directory(
        session, context=context, route=route, draft_id=draft.id, media_kind="CAROUSEL"
    )
    assert video.verified_account_count == 3
    assert carousel.verified_account_count == 0
    assert carousel.state == "PENDING"
