"""Minis专用自动定向合同，防止通用MANUAL合同再次进入创建。"""

from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.builds.request_compiler import create_arguments, decode_intent
from app.modules.builds.scene_schemas import SceneContext
from app.modules.builds.targeting import apply_targeting
from app.modules.builds.targeting_schemas import AudienceTargeting

ADULTS = ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"]


def scene():
    return SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="verified",
        field_constraints={
            "target_regions": [{"region_code": "BR", "location_id": "3469034"}]
        },
    )


def body(**changes):
    return {
        "advertiser_id": "100",
        "campaign_id": "200",
        "adgroup_name": "diagnostic",
        "minis_id": "mini",
        "roas_bid": "1.08",
        "schedule_start_time": "2026-11-09 00:00:00",
        "targeting_optimization_mode": "AUTOMATIC",
        "targeting_spec": {
            "location_ids": ["3469034"],
            "languages": ["en"],
            "spc_audience_age": "OVER_TWENTY_FIVE",
        },
        **changes,
    }


@pytest.mark.parametrize(
    "ages,want",
    [
        ([], "OVER_EIGHTEEN"),
        (ADULTS, "OVER_EIGHTEEN"),
        (ADULTS[1:], "OVER_TWENTY_FIVE"),
    ],
)
def test_minis_language_and_minimum_age_stay_automatic(ages, want):
    result = apply_targeting(
        scene(), AudienceTargeting(languages=["en"], age_groups=ages), ["BR"]
    )
    assert result.supported
    assert result.adgroup_fields["targeting_optimization_mode"] == "AUTOMATIC"
    assert dict(result.adgroup_fields["targeting_spec"]) == {
        "location_ids": ("3469034",),
        "languages": ("en",),
        "spc_audience_age": want,
    }


@pytest.mark.parametrize(
    "selected", [{"age_groups": ["AGE_25_34"]}, {"gender": "GENDER_FEMALE"}]
)
def test_unsupported_old_conditions_block_without_dropping_intent(selected):
    result = apply_targeting(scene(), AudienceTargeting(**selected), ["BR"])
    assert not result.supported
    assert "targeting_minis_unsupported" in result.reason_codes
    assert result.field_constraints["audience_targeting"][next(iter(selected))]


@pytest.mark.parametrize("channel", ["OFFICIAL_API", "OFFICIAL_MCP"])
def test_minimum_age_and_language_reach_wire_in_both_channels(channel):
    intent = decode_intent("ADGROUP", body())
    _, wire = create_arguments(attempt_id=uuid4(), intent=intent, channel=channel)
    assert wire["targeting_optimization_mode"] == "AUTOMATIC"
    assert wire["targeting_spec"] == {
        "location_ids": ["3469034"],
        "languages": ["en"],
        "spc_audience_age": "OVER_TWENTY_FIVE",
    }


def test_old_frozen_manual_is_readable_but_not_sendable():
    legacy = body(
        targeting_optimization_mode="MANUAL",
        targeting_spec={"location_ids": ["3469034"], "age_groups": ["AGE_25_34"]},
    )
    intent = decode_intent("ADGROUP", legacy)
    with pytest.raises(DomainError) as err:
        create_arguments(attempt_id=uuid4(), intent=intent, channel="OFFICIAL_API")
    assert err.value.code == "targeting_minis_unsupported"


def test_minimum_age_readback_must_be_observed_and_match():
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
    )
    from app.modules.builds.readback_compare import parse_page

    intent = decode_intent("ADGROUP", body(operation_status="DISABLE"))
    from app.modules.builds.request_compiler import encode_intent

    row = {**encode_intent(intent), "adgroup_id": "300"}

    def read():
        return parse_page(
            query=BuildReadQuery(intent=intent, remote_id="300"),
            response=McpBusinessResponse(
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
            ),
        ).rows[0]

    assert read().intent == intent
    row["targeting_spec"]["spc_audience_age"] = "OVER_EIGHTEEN"
    assert read().intent != intent
    del row["targeting_spec"]["spc_audience_age"]
    assert read().intent is None


@pytest.mark.parametrize(
    "selected", [{"age_groups": ["AGE_25_34"]}, {"gender": "GENDER_FEMALE"}]
)
def test_strategy_validation_rejects_unsupported_targeting(session, context, selected):
    from app.modules.strategies.service import validate_strategy
    from tests.modules.strategies.test_versions import config

    value = config().model_copy(update={"targeting": AudienceTargeting(**selected)})
    issues = validate_strategy(session, context=context, config=value)
    assert any(
        issue.field == "targeting" and issue.code == "targeting_minis_unsupported"
        for issue in issues
    )


def test_unsupported_targeting_is_a_public_validation_error_not_unknown_outcome():
    import asyncio
    import json

    from app.core.errors import domain_error_handler

    error = DomainError("targeting_minis_unsupported", "private diagnostic text")
    response = asyncio.run(domain_error_handler(None, error))
    assert response.status_code == 422
    payload = json.loads(response.body)
    assert payload["code"] == "targeting_minis_unsupported"
    assert "private diagnostic text" not in payload["message"]


def test_standard_status_cannot_supply_missing_minimum_age():
    from app.integrations.tiktok.contracts.builds import AdGroupStatus, BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
    )
    from app.modules.builds.readback_compare import parse_page, with_adgroup_status
    from app.modules.builds.request_compiler import encode_intent

    intent = decode_intent("ADGROUP", body())
    row = {**encode_intent(intent), "adgroup_id": "300"}
    row.pop("operation_status", None)
    del row["targeting_spec"]["spc_audience_age"]
    record = parse_page(
        query=BuildReadQuery(intent=intent, remote_id="300"),
        response=McpBusinessResponse(
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
        ),
    ).rows[0]
    with pytest.raises(DomainError):
        with_adgroup_status(
            record=record,
            status=AdGroupStatus(
                advertiser_id="100",
                adgroup_id="300",
                operation_status="ENABLE",
                evidence=CallEvidence(),
            ),
        )


@pytest.mark.parametrize("mode,blocked", [(None, False), ("MANUAL", True)])
def test_legacy_automatic_scene_defaults_preserve_inflight_intent(
    monkeypatch, mode, blocked
):
    from types import SimpleNamespace

    from app.modules.builds import execution

    original = apply_targeting(scene(), AudienceTargeting(), ["BR"]).to_snapshot()
    original["adgroup_fields"].pop("targeting_optimization_mode")
    original["adgroup_fields"]["targeting_spec"].pop("spc_audience_age")
    if mode:
        original["adgroup_fields"]["targeting_optimization_mode"] = mode
    frozen = SimpleNamespace(
        native_request=None,
        scene_snapshot=original,
        bc_id="bc",
        advertiser_id="100",
        link_id=uuid4(),
        preview_id=uuid4(),
        media_kind="VIDEO",
        budget_strategy="SERIES",
        bid_strategy="TARGET_ROAS",
    )
    monkeypatch.setattr(execution, "read_scene_context", lambda *a, **k: scene())
    monkeypatch.setattr(execution, "load_preview_route", lambda *a, **k: None)
    if blocked:
        with pytest.raises(DomainError) as error:
            execution._current_scene(None, None, frozen)
        assert error.value.code == "scene_intent_changed"
    else:
        execution._current_scene(None, None, frozen)
        assert "targeting_optimization_mode" not in original["adgroup_fields"]
    assert "spc_audience_age" not in original["adgroup_fields"]["targeting_spec"]
