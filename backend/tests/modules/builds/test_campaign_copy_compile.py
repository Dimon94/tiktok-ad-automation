import pytest

from app.core.errors import DomainError
from app.modules.builds.copy_compile import observed_body


def campaign():
    return {
        "advertiser_id": "a",
        "campaign_id": "c",
        "campaign_name": "当前名称",
        "objective_type": "APP_PROMOTION",
        "app_promotion_type": "MINIS",
        "campaign_type": "REGULAR_CAMPAIGN",
        "budget": "120",
        "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
        "budget_optimize_on": True,
        "operation_status": "DISABLE",
    }


def test_current_source_budget_is_preserved_without_history():
    value = campaign()
    assert observed_body("CAMPAIGN", value)["budget"] == "120"
    assert value == campaign()


@pytest.mark.parametrize(
    "field",
    ["objective_type", "app_promotion_type", "budget_mode", "budget_optimize_on"],
)
def test_missing_current_fields_are_not_filled_by_dto_defaults(field):
    value = campaign()
    del value[field]
    with pytest.raises(DomainError, match="来源"):
        observed_body("CAMPAIGN", value)


def test_unknown_business_setting_is_not_silently_dropped():
    value = campaign()
    value["unsupported_delivery_setting"] = True
    with pytest.raises(DomainError):
        observed_body("CAMPAIGN", value)


def test_untrusted_binary_money_is_not_relabelled_exact():
    value = campaign()
    value["budget"] = 120.1
    with pytest.raises(DomainError):
        observed_body("CAMPAIGN", value)


def test_malformed_creative_is_a_visible_blocker():
    body = {
        "advertiser_id": "a",
        "adgroup_id": "g",
        "ad_name": "ad",
        "operation_status": "ENABLE",
        "creative_list": [None],
        "ad_text_list": [],
        "landing_page_url_list": [],
        "ad_configuration": {},
    }
    with pytest.raises(DomainError):
        observed_body("AD", body)


def test_empty_platform_video_file_name_is_omitted_for_creation():
    body = {
        "advertiser_id": "a",
        "adgroup_id": "g",
        "ad_name": "ad",
        "operation_status": "ENABLE",
        "creative_list": [
            {
                "creative_info": {
                    "ad_format": "SINGLE_VIDEO",
                    "identity_type": "BC_AUTH_TT",
                    "identity_id": "identity",
                    "identity_authorized_bc_id": "bc",
                    "video_info": {"video_id": "video", "file_name": ""},
                    "image_info": [{"web_uri": "cover"}],
                }
            }
        ],
        "ad_text_list": [{"ad_text": "text"}],
        "landing_page_url_list": [{"landing_page_url": "https://example.com"}],
        "ad_configuration": {"call_to_action_id": "cta"},
    }
    result = observed_body("AD", body)
    assert "file_name" not in result["creative_list"][0]["creative_info"]["video_info"]
    assert body["creative_list"][0]["creative_info"]["video_info"]["file_name"] == ""


def group():
    return {
        "advertiser_id": "a",
        "campaign_id": "c",
        "adgroup_name": "g",
        "minis_id": "m",
        "promotion_type": "MINI_APP",
        "optimization_goal": "VALUE",
        "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
        "vbo_window": "ZERO_DAY",
        "bid_type": "BID_TYPE_NO_BID",
        "deep_bid_type": "VO_HIGHEST_VALUE",
        "billing_event": "OCPM",
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": "2026-11-10 00:00:00",
        "targeting_spec": {
            "location_ids": ["loc"],
            "spc_audience_age": "OVER_EIGHTEEN",
        },
        "targeting_optimization_mode": "AUTOMATIC",
        "operation_status": "ENABLE",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"budget": "100"},
        {"schedule_type": "SCHEDULE_START_END"},
        {"budget": "100", "budget_mode": None},
    ],
)
def test_conditional_fields_cannot_be_invented_from_dto_defaults(changes):
    with pytest.raises(DomainError) as error:
        observed_body("ADGROUP", {**group(), **changes})
    assert error.value.code == "copy_source_incomplete"


@pytest.mark.parametrize("field", ["budget", "budget_optimize_on"])
def test_campaign_null_budget_facts_are_not_creation_defaults(field):
    with pytest.raises(DomainError):
        observed_body("CAMPAIGN", {**campaign(), field: None})


def test_non_search_flag_from_platform_is_not_a_copy_blocker():
    assert (
        observed_body("CAMPAIGN", {**campaign(), "is_search_campaign": False})["budget"]
        == "120"
    )


def test_platform_group_defaults_preserve_target_roas_without_group_budget():
    value = {
        **group(),
        "budget": 0,
        "budget_mode": "BUDGET_MODE_INFINITE",
        "min_budget": 0,
        "roas_bid": "1.06",
        "deep_bid_type": "VO_MIN_ROAS",
        "conversion_bid_price": 0,
        "schedule_end_time": "2036-10-10 05:00:00",
        "targeting_spec": {
            **group()["targeting_spec"],
            "age_groups": [
                "AGE_18_24",
                "AGE_25_34",
                "AGE_35_44",
                "AGE_45_54",
                "AGE_55_100",
            ],
            "gender": "GENDER_UNLIMITED",
            "spending_power": "ALL",
            "brand_safety_type": "NO_BRAND_SAFETY",
        },
    }
    result = observed_body("ADGROUP", value)
    assert result["roas_bid"] == "1.06"
    assert "budget" not in result
    assert "schedule_end_time" not in result
    assert "age_groups" not in result["targeting_spec"]


def test_suggestion_audience_setting_is_preserved_for_copy():
    value = {**group(), "suggestion_audience_enabled": False}

    result = observed_body("ADGROUP", value)

    assert result["suggestion_audience_enabled"] is False


def test_unknown_setting_reports_layer_and_field_without_exposing_value():
    with pytest.raises(DomainError) as error:
        observed_body("CAMPAIGN", {**campaign(), "new_delivery_rule": "private-value"})
    assert "系列" in error.value.message
    assert "new_delivery_rule" in error.value.message
    assert "private-value" not in error.value.message
