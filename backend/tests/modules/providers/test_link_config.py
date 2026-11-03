from types import SimpleNamespace

import pytest

from app.core.errors import DomainError
from app.modules.providers.capabilities import (
    capabilities_for_kind,
    effective_link_config,
    validate_link_values,
)


def app(**channel):
    return SimpleNamespace(channel_config=channel, link_defaults={})


def names(kind, application=None):
    return [f.name for f in capabilities_for_kind(kind, application).fields]


def test_fields_match_provider_backends():
    assert names("jiashu") == ["episode"]
    assert names("wangyan") == ["episode"]
    assert names("minglue") == ["episode"]
    assert names("duiba") == ["episode", "card_point_episode"]
    assert names("gangganhao", app(delivery_mode="iaa")) == [
        "free_episode_count",
        "episode_seq",
        "payment_template_id",
    ]
    assert names("rongliang") == []


def test_per_drama_fields_have_rule_and_no_default():
    schema = capabilities_for_kind("duiba")
    card = next(f for f in schema.fields if f.name == "card_point_episode")
    assert card.source == "per_drama" and card.default is None
    assert card.rule == "按剧免费集数"
    free = capabilities_for_kind("gangganhao", app()).fields[0]
    assert free.rule == "按剧免费集数，缺失时为 3"


@pytest.mark.parametrize("mode", ["iaa", "iap", "mixed"])
def test_payment_template_options_and_requirement(mode):
    application = app(
        delivery_mode=mode,
        payment_templates=[{"id": "7", "name": "周卡"}],
    )
    field = capabilities_for_kind("gangganhao", application).fields[2]
    assert field.required is (mode in {"iap", "mixed"})
    assert [(o.value, o.label) for o in field.options] == [("7", "周卡")]


def test_defaults_reject_per_drama_and_unknown_keys():
    with pytest.raises(DomainError):
        validate_link_values("duiba", app(), {"card_point_episode": 3}, target="defaults")
    with pytest.raises(DomainError):
        validate_link_values("wangyan", app(), {"promote_name": "x"}, target="overrides")
    with pytest.raises(DomainError):
        validate_link_values("rongliang", app(), {"alias": "x"}, target="overrides")


def test_null_and_blank_values_mean_not_overridden():
    assert validate_link_values(
        "duiba", app(), {"episode": None, "card_point_episode": ""}, target="overrides"
    ) == {}


def test_integer_and_template_validation():
    with_templates = app(payment_templates=[{"id": "7", "name": "周卡"}])
    assert validate_link_values(
        "gangganhao",
        with_templates,
        {"free_episode_count": "0", "payment_template_id": "7"},
        target="overrides",
    ) == {"free_episode_count": 0, "payment_template_id": 7}
    bad_cases = (
        (app(), {"episode_seq": 0}),
        (app(), {"free_episode_count": -1}),
        (with_templates, {"payment_template_id": "9"}),
        (app(), {"episode_seq": True}),
    )
    for application, bad in bad_cases:
        with pytest.raises(DomainError):
            validate_link_values("gangganhao", application, bad, target="overrides")


def test_template_without_options_is_not_membership_checked():
    # 没有模板选项时无法在本地核实，交给刚刚好建链接口校验。
    assert validate_link_values(
        "gangganhao", app(), {"payment_template_id": 7}, target="overrides"
    ) == {"payment_template_id": 7}


def test_effective_config_priority():
    application = SimpleNamespace(
        channel_config={"delivery_mode": "iaa"}, link_defaults={"episode": 3}
    )
    assert effective_link_config("duiba", application, {}) == {"episode": 3}
    assert effective_link_config("duiba", application, {"episode": 5, "card_point_episode": 6}) == {
        "episode": 5,
        "card_point_episode": 6,
    }
    assert effective_link_config("jiashu", app(), {}) == {"episode": 1}
    assert effective_link_config("rongliang", app(), {}) == {}
    # 已合并结果再次合并保持不变：草稿与准备请求都会经过合并入口。
    once = effective_link_config("duiba", application, {"card_point_episode": 6})
    assert effective_link_config("duiba", application, once) == once


def test_paid_application_requires_template_after_merge():
    paid = SimpleNamespace(
        channel_config={"delivery_mode": "iap", "payment_templates": [{"id": "7", "name": "周卡"}]},
        link_defaults={},
    )
    with pytest.raises(DomainError) as error:
        effective_link_config("gangganhao", paid, {})
    assert error.value.code == "link_config_template_required"
    paid.link_defaults = {"payment_template_id": 7}
    assert effective_link_config("gangganhao", paid, {})["payment_template_id"] == 7


def test_link_config_errors_are_public_422():
    from app.core.errors import ERROR_HTTP_STATUS, ERROR_PUBLIC_MESSAGES

    for code in ("link_config_invalid", "link_config_template_required"):
        assert ERROR_HTTP_STATUS[code] == 422 and ERROR_PUBLIC_MESSAGES[code]
