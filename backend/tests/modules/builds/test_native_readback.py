"""Actual response oracle is independent of the create body."""

import pytest

from app.integrations.tiktok.contracts.builds import BuildReadQuery
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.modules.builds.readback_compare import compare_record, parse_page
from app.modules.builds.request_compiler import decode_intent


def group_body():
    return {
        "advertiser_id": "account",
        "campaign_id": "campaign",
        "adgroup_name": "group",
        "promotion_type": "NATIVE_SERIES",
        "native_series_id": "series",
        "identity_type": "TT_USER",
        "identity_id": "owner",
        "targeting_spec": {"location_ids": ["6252001"]},
        "schedule_start_time": "2027-01-01 00:00:00",
        "schedule_type": "SCHEDULE_FROM_NOW",
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],
        "optimization_goal": "VALUE",
        "optimization_event": "AD_REVENUE_VALUE",
        "deep_bid_type": "VO_MIN_ROAS",
        "bid_type": "BID_TYPE_NO_BID",
        "billing_event": "OCPM",
        "roas_bid": "1.2",
        "vbo_window": "ZERO_DAY",
        "operation_status": "ENABLE",
    }


def observed_group():
    return {
        "advertiser_id": "account",
        "campaign_id": "campaign",
        "adgroup_id": "remote",
        "adgroup_name": "group",
        "promotion_type": "NATIVE_SERIES",
        "native_series_id": "series",
        "identity_type": "TT_USER",
        "identity_id": "owner",
        "targeting_spec": {"location_ids": ["6252001"]},
        "schedule_start_time": "2027-01-01 00:00:00",
        "schedule_type": "SCHEDULE_FROM_NOW",
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],
        "optimization_goal": "VALUE",
        "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
        "deep_bid_type": "VO_MIN_ROAS",
        "bid_type": "BID_TYPE_NO_BID",
        "billing_event": "OCPM",
        "roas_bid": "1.2",
        "vbo_window": "ZERO_DAY",
        "operation_status": "ENABLE",
    }


def creative(cover="cover"):
    return {
        "creative_info": {
            "identity_type": "TT_USER",
            "identity_id": "actor",
            "ad_format": "SINGLE_VIDEO",
            "video_info": {"video_id": "video"},
            "image_info": [{"web_uri": cover}],
        }
    }


def ad_body():
    return {
        "advertiser_id": "account",
        "adgroup_id": "group",
        "ad_name": "ad",
        "creative_list": [creative(), creative("cover-two")],
        "ad_text_list": [{"ad_text": "copy"}],
        "ad_configuration": {"call_to_action_id": "cta"},
        "operation_status": "ENABLE",
    }


def observed_ad():
    return {
        "advertiser_id": "account",
        "adgroup_id": "group",
        "smart_plus_ad_id": "remote",
        "ad_name": "ad",
        "creative_list": [creative("cover-two"), creative()],
        "ad_text_list": [{"ad_text": "copy"}],
        "ad_configuration": {"call_to_action_id": "cta"},
        "operation_status": "ENABLE",
    }


def compare(kind, body, actual):
    q = BuildReadQuery(intent=decode_intent(kind, body), remote_id="remote")
    page = parse_page(
        query=q,
        response=McpBusinessResponse(
            data={
                "list": [actual],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            },
            evidence=CallEvidence(),
        ),
    )
    return compare_record(query=q, record=page.rows[0])


def test_native_event_mapping_and_creative_multiset():
    assert compare("ADGROUP", group_body(), observed_group()) == "MATCH"
    assert compare("AD", ad_body(), observed_ad()) == "MATCH"
    row = observed_group()
    row["optimization_event"] = "ACTIVE_PAY"
    assert compare("ADGROUP", group_body(), row) == "MISMATCH"


@pytest.mark.parametrize("key", list(observed_group()))
@pytest.mark.parametrize("mode", ["missing", "changed"])
def test_every_group_fact_required(key, mode):
    row = observed_group()
    if mode == "missing":
        del row[key]
    else:
        row[key] = (
            {"location_ids": ["other"]}
            if key == "targeting_spec"
            else ["other"]
            if key == "placements"
            else "other"
        )
    if key == "adgroup_id" and mode == "missing":
        with pytest.raises(RemoteCallError):
            compare("ADGROUP", group_body(), row)
    else:
        assert compare("ADGROUP", group_body(), row) != "MATCH"


@pytest.mark.parametrize(
    "path",
    [
        ("advertiser_id",),
        ("adgroup_id",),
        ("operation_status",),
        ("ad_text_list", 0, "ad_text"),
        ("ad_configuration", "call_to_action_id"),
        ("creative_list", 0, "creative_info", "identity_id"),
        ("creative_list", 0, "creative_info", "identity_type"),
        ("creative_list", 0, "creative_info", "ad_format"),
        ("creative_list", 0, "creative_info", "video_info", "video_id"),
        ("creative_list", 0, "creative_info", "image_info", 0, "web_uri"),
    ],
)
@pytest.mark.parametrize("mode", ["missing", "changed"])
def test_every_creative_fact_required(path, mode):
    row = observed_ad()
    parent = row
    for key in path[:-1]:
        parent = parent[key]
    if mode == "missing":
        del parent[path[-1]]
    else:
        parent[path[-1]] = "other"
    assert compare("AD", ad_body(), row) != "MATCH"


def test_duplicate_assets_and_forbidden_group_budget():
    row = observed_ad()
    row["creative_list"] = [creative(), creative()]
    assert compare("AD", ad_body(), row) == "MISMATCH"
    row = observed_group()
    row["budget"] = "100"
    assert compare("ADGROUP", group_body(), row) != "MATCH"


def test_creative_permutations_preserve_multiplicity_and_exact_text():
    from itertools import permutations

    body = ad_body()
    for values in permutations([creative(), creative("cover-two")]):
        row = observed_ad()
        row["creative_list"] = list(values)
        assert compare("AD", body, row) == "MATCH"
    row = observed_ad()
    row["ad_text_list"].append({"ad_text": "extra"})
    assert compare("AD", body, row) != "MATCH"
    row = observed_ad()
    row["creative_list"][0]["creative_info"]["identity_authorized_bc_id"] = "unexpected"
    assert compare("AD", body, row) != "MATCH"


@pytest.mark.parametrize(
    "key",
    [
        "advertiser_id",
        "objective_type",
        "app_promotion_type",
        "budget",
        "budget_mode",
        "budget_optimize_on",
        "operation_status",
    ],
)
@pytest.mark.parametrize("mode", ["missing", "changed"])
def test_campaign_facts(key, mode):
    body = {
        "advertiser_id": "account",
        "campaign_name": "campaign",
        "budget": "1000.01",
        "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
        "budget_optimize_on": True,
        "operation_status": "ENABLE",
        "objective_type": "APP_PROMOTION",
        "app_promotion_type": "MINIS",
        "campaign_type": "REGULAR_CAMPAIGN",
    }
    row = {
        "advertiser_id": "account",
        "campaign_name": "campaign",
        "campaign_id": "remote",
        "budget": "1000.01",
        "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
        "budget_optimize_on": True,
        "operation_status": "ENABLE",
        "objective_type": "APP_PROMOTION",
        "app_promotion_type": "MINIS",
        "campaign_type": "REGULAR_CAMPAIGN",
    }
    assert compare("CAMPAIGN", body, row) == "MATCH"
    if mode == "missing":
        del row[key]
    else:
        row[key] = (
            "1000.02"
            if key == "budget"
            else False
            if key == "budget_optimize_on"
            else "other"
        )
    assert compare("CAMPAIGN", body, row) != "MATCH"


@pytest.mark.parametrize(
    "case", ["empty", "duplicate", "bad_page", "bad_type", "wrong_id", "empty_object"]
)
def test_invalid_readback_cannot_be_complete(case):
    from app.modules.builds.reconciliation_scan import advance

    row = observed_group()
    q = BuildReadQuery(
        intent=decode_intent("ADGROUP", group_body()), remote_id="remote"
    )
    data = {
        "list": [row],
        "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 1},
    }
    if case == "empty":
        data["list"] = []
        data["page_info"]["total_number"] = 0
    if case == "duplicate":
        data["list"] = [row, row]
        data["page_info"]["total_number"] = 2
    if case == "bad_page":
        data["page_info"]["total_page"] = 2
    if case == "bad_type":
        row["roas_bid"] = True
    if case == "wrong_id":
        row["adgroup_id"] = "other"
    if case == "empty_object":
        data["list"] = [{}]
    try:
        page = parse_page(
            query=q, response=McpBusinessResponse(data=data, evidence=CallEvidence())
        )
        _, state = advance(
            query=q,
            progress={"stage": "OBJECT", "page": 1, "seen": 0, "matches": 0},
            page=page,
            seen_before=lambda _: False,
        )
        assert state != "COMPLETE"
    except RemoteCallError:
        pass
    except Exception as error:
        from app.core.errors import DomainError

        assert isinstance(error, DomainError)


def test_native_unrequested_schedule_and_targeting_changes_are_not_ignored():
    row = observed_group()
    row["schedule_end_time"] = "2027-01-02 00:00:00"
    assert compare("ADGROUP", group_body(), row) != "MATCH"
    row = observed_group()
    row["targeting_spec"]["gender"] = "GENDER_MALE"
    assert compare("ADGROUP", group_body(), row) != "MATCH"


@pytest.mark.parametrize(
    "status", ["AUDIT_REJECTED", "AD_STATUS_AUDIT_DENY", "DISAPPROVED", {}, 123]
)
def test_explicit_review_rejection_or_invalid_status_cannot_match(status):
    row = observed_ad()
    row["secondary_status"] = status
    assert compare("AD", ad_body(), row) != "MATCH"


@pytest.mark.parametrize(
    "kind,key,value",
    [
        (
            "ADGROUP",
            "drama_selling_setting",
            {
                "type": "DISTRIBUTION",
                "distribution_link": "https://example.test/distribution",
            },
        ),
        ("AD", "call_to_action_list", [{"call_to_action": "SHOP_NOW"}]),
        ("AD", "interactive_add_on_list", [{"interactive_add_on_id": "unexpected"}]),
    ],
)
def test_direct_mode_observed_forbidden_fields(kind, key, value):
    row = observed_group() if kind == "ADGROUP" else observed_ad()
    row[key] = value
    assert (
        compare(kind, group_body() if kind == "ADGROUP" else ad_body(), row)
        == "MISMATCH"
    )
