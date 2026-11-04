from uuid import UUID

from app.modules.business.identity import identify_campaign

P = "11111111-1111-4111-8111-111111111111"
D = "22222222-2222-4222-8222-222222222222"


def catalog():
    return {
        "providers": [{"id": P, "kind": "jiashu", "name": "甲-嘉书"}],
        "dramas": [
            {
                "id": D,
                "provider_id": P,
                "external_id": "108345",
                "display_id": None,
                "title": "Known drama",
            }
        ],
    }


def test_historical_campaign_uses_unique_provider_and_stable_number():
    result = identify_campaign("jiashu-Old drama title-108345-1016-01-owner", catalog())
    assert result["drama_id"] == UUID(D)
    assert result["drama_key"] == D
    assert result["provider_id"] == UUID(P)
    assert result["drama_name"] == "Known drama"


def test_braced_series_identity_checks_repeated_number_and_provider():
    data = catalog()
    data["providers"][0]["kind"] = "wangyan"
    assert identify_campaign(
        "{b108345/s900/c4}-剧目-108345-1016-01-owner-wangyan", data
    )["drama_id"] == UUID(D)
    assert (
        identify_campaign("{b999/s900/c4}-剧目-108345-1016-01-owner-wangyan", data)[
            "drama_id"
        ]
        is None
    )


def test_labels_and_ambiguous_catalog_never_claim_an_unproven_drama():
    assert (
        identify_campaign("jiashu-Unlisted drama-random", catalog())["drama_id"] is None
    )
    assert (
        identify_campaign("other-Known drama-108345-1016-01", catalog())["provider_id"]
        is None
    )
    data = catalog()
    data["dramas"].append(
        {**data["dramas"][0], "id": "33333333-3333-4333-8333-333333333333"}
    )
    assert (
        identify_campaign("jiashu-Known drama-108345-1016-01", data)["drama_id"] is None
    )
    data = catalog()
    data["providers"].append(
        {**data["providers"][0], "id": "33333333-3333-4333-8333-333333333333"}
    )
    assert (
        identify_campaign("jiashu-Known drama-108345-1016-01", data)["provider_id"]
        is None
    )


def test_ambiguous_number_positions_do_not_claim_a_catalog_drama():
    assert (
        identify_campaign("jiashu-Season-42-0101-archive-108345-1101-01", catalog())[
            "drama_id"
        ]
        is None
    )


def test_report_name_identifies_provider_even_without_drama_catalog():
    data = catalog()
    result = identify_campaign("jiashu-Historical drama-999999-1101-01-owner", data)
    assert result["provider_id"] == UUID(P)
    assert result["drama_id"] is None
    assert result["drama_key"] == f"external:{P}:999999"
    named = identify_campaign("jiashu-Name only-remarks", data)
    assert named["provider_id"] == UUID(P)
    assert named["drama_name"] == "Name only"
    assert named["drama_key"].startswith("named:")


def test_report_labels_resolve_other_provider_by_exact_display_label():
    data = {"providers": [{"id": P, "kind": "other", "name": "xuyin"}], "dramas": []}
    result = identify_campaign("xuyin-SweetReel-XYTop50-LOCAL-20261026", data)
    assert result["provider_id"] == UUID(P) and result["drama_name"] == "SweetReel"
    assert result["drama_key"].startswith("named:")


def test_historical_explicit_gangganghao_aliases():
    data = {
        "providers": [{"id": P, "kind": "gangganhao", "name": "刚刚好连接"}],
        "dramas": [],
    }
    for label in ("ggh", "gangganghao", "gangganhao", "刚刚好"):
        result = identify_campaign(f"{label}-Historical title-787-20261003-owner", data)
        assert result["provider_id"] == UUID(P)
        assert result["drama_name"] == "Historical title"


def test_number_identity_trims_name_without_changing_title():
    result = identify_campaign(
        "  jiashu-Unlisted historical drama-999999-1101-01-owner  ", catalog()
    )
    assert result["drama_name"] == "Unlisted historical drama"
    assert result["drama_key"] == f"external:{P}:999999"


def test_multiple_number_positions_cannot_fall_back_to_matching_catalog_title():
    result = identify_campaign(
        "jiashu-Known drama-999-1101-note-108345-1102-owner", catalog()
    )
    assert result["provider_id"] == UUID(P)
    assert result["drama_id"] is None
    assert result["drama_key"].startswith("named:")
