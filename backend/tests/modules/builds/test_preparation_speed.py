"""准备阶段只读取必要事实；预算和竞价按本次策略校验。"""

from dataclasses import replace
from types import SimpleNamespace

from app.modules.builds.preview_validation import scene_reasons
from app.modules.builds.scene import _assemble_scene
from app.modules.builds.scene_constraints import constraints_for
from tests.modules.builds.test_strategy_payloads import _config, _scene


def test_carousel_format_can_submit_without_prior_platform_verification():
    constraints, reasons = constraints_for("USD", media_kind="CAROUSEL")
    assert reasons == ()
    assert constraints["media_kind"] == "CAROUSEL"
    assert constraints["max_images_per_unit"] == 35
    scene = replace(_scene(**constraints), media_kind="CAROUSEL")
    assert not scene_reasons(_config(), scene, "USD", media_kind="CAROUSEL")


def test_new_account_group_budget_does_not_require_historical_adgroups():
    constraints, reasons = constraints_for("USD")
    assert not reasons
    assert not scene_reasons(
        _config(budget_strategy="ADGROUP", bid_strategy="HIGHEST_VALUE"),
        _scene(**constraints),
        "USD",
    )


def test_minimum_roas_ineligibility_only_blocks_target_roas():
    assembled = _assemble_scene(
        scope={
            "media_kind": "VIDEO",
            "access": SimpleNamespace(currency="USD"),
            "minis_id": "mini",
            "basis": "scope",
        },
        facts={"vbo": {"vo_min_roas": "UNQUALIFIED", "vo_status": "QUALIFIED"}},
        reasons=[],
        evidence_ids=(),
        capability=SimpleNamespace(scope_verified=True, can_build=True),
        locally_operable=True,
    )
    assert "minis_vbo_unverified" not in assembled.reason_codes
    scene = _scene(**assembled.field_constraints)
    assert "minis_vbo_unverified" not in scene_reasons(
        _config(bid_strategy="HIGHEST_VALUE"), scene, "USD"
    )
    assert "minis_vbo_unverified" in scene_reasons(
        _config(bid_strategy="TARGET_ROAS", target_roas="1.1"), scene, "USD"
    )
