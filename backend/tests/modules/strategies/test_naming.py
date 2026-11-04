from uuid import UUID

import pytest

from app.core.errors import DomainError
from app.modules.strategies.naming import local_display_drama_id, render_names

ARGS = {
    "protected_base": "{b30008/s328302/c3}-The Bond",
    "title": "The Bond",
    "provider_pinyin": "jiashu",
    "display_drama_id": "106001",
    "date_text": "20260913",
    "batch_short_id": "A7K2",
    "group_no": 1,
    "creative_no": 2,
    "max_length": 200,
}


@pytest.mark.parametrize("protected", ["", ARGS["protected_base"]])
def test_unified_name_keeps_display_id_and_automatically_appends_batch(protected):
    base = protected or "jiashu-The Bond"
    names = render_names(**{**ARGS, "protected_base": protected})
    campaign = f"{base}-106001-A7K2"
    assert names == (campaign, campaign + "-g01", campaign + "-g01-sp2")


@pytest.mark.parametrize("protected", ["", ARGS["protected_base"]])
def test_custom_date_order_and_literal_text_apply_to_both_providers(protected):
    base = protected or "jiashu-The Bond"
    names = render_names(
        **{**ARGS, "protected_base": protected},
        template="{provider_drama}-测试-{YYYYMMDD}-{drama_id}",
    )
    assert names[0] == f"{base}-测试-20260913-106001-A7K2"
    assert names[2] == names[0] + "-g01-sp2"


@pytest.mark.parametrize(
    "template",
    [
        "{provider_drama}",
        "{drama_id}",
        "{{provider_drama}}-{drama_id}",
        "{provider_drama}-{drama_id.__class__}",
        "{provider_drama}-{drama_id[0]}",
        "{provider_drama}-{drama_id!r}",
        "{provider_drama}-{drama_id:12}",
        "{provider_drama}-{drama_id}-\n",
        "{provider_drama}-{drama_id}-{random}",
        "{provider_drama}-{drama_id}-{batch_short_id}",
        "{provider_drama}-{drama_id}-{unknown}",
        "{provider_drama}-{drama_id}-{drama_id}",
        "{provider_drama}-{drama_id}-{YYYYMMDD}-{YYYYMMDD}",
        "{provider_drama}-{drama_id}-{provider_pinyin}-{provider_pinyin}",
        "-{",
        "-{}",
    ],
)
def test_template_requires_both_fields_and_rejects_manual_batch_or_unsafe_variables(
    template,
):
    with pytest.raises(DomainError, match="invalid_name_template"):
        render_names(**ARGS, template=template)


def test_names_never_truncate_protected_prefix():
    with pytest.raises(DomainError, match="name_too_long"):
        render_names(**{**ARGS, "max_length": 15})


def test_inserted_values_are_literal():
    names = render_names(
        **{**ARGS, "protected_base": "", "title": "A {random} Story"},
        template="{provider_drama}-{{literal}}-{drama_id}",
    )
    assert names[0] == "jiashu-A {random} Story-{literal}-106001-A7K2"


@pytest.mark.parametrize("protected", ["", ARGS["protected_base"]])
@pytest.mark.parametrize(
    "drama_id", ["", "0", "-1", "32827A", "３２８２７", "ada-", "ada-12a", "ADA-1"]
)
def test_attribution_prefix_requires_ascii_numeric_display_drama_id(
    protected, drama_id
):
    with pytest.raises(DomainError) as error:
        render_names(
            **{
                **ARGS,
                "protected_base": protected,
                "display_drama_id": drama_id,
            }
        )
    assert error.value.code == "naming_context_missing"


def test_manual_drama_without_provider_id_uses_stable_local_number():
    drama = UUID("12345678-1234-5678-1234-567812345678")
    local_id = local_display_drama_id(drama)
    assert local_id == local_display_drama_id(drama)
    assert local_id.startswith("ada-") and len(local_id) == 12
    names = render_names(**{**ARGS, "protected_base": "", "display_drama_id": local_id})
    assert names[0] == f"jiashu-The Bond-{local_id}-A7K2"


@pytest.mark.parametrize(
    ("provider", "protected"),
    [("jiashu", ""), ("wangyan", ARGS["protected_base"]), ("rongliang", "RL-The Bond")],
)
def test_provider_pinyin_tag_uses_provider_context_and_preserves_attribution(
    provider, protected
):
    names = render_names(
        **{**ARGS, "provider_pinyin": provider, "protected_base": protected},
        template="{provider_drama}-{drama_id}-{YYYYMMDD}-{provider_pinyin}",
    )
    base = protected or f"{provider}-The Bond"
    campaign = f"{base}-106001-20260913-{provider}-A7K2"
    assert names == (campaign, campaign + "-g01", campaign + "-g01-sp2")


@pytest.mark.parametrize(
    "protected,label",
    [
        ("", "jiashu"),
        (ARGS["protected_base"], "{b30008/s328302/c3}"),
        ("opaque-marker", "opaque-marker"),
    ],
)
@pytest.mark.parametrize(
    "template,expected",
    [
        (
            "{drama_name}-{provider_label}-{drama_id}-{YYYYMMDD}",
            "The Bond-{label}-106001-20260913-A7K2",
        ),
        (
            "{YYYYMMDD}_{drama_id}_{provider_label}_{drama_name}",
            "20260913_106001_{label}_The Bond-A7K2",
        ),
    ],
)
def test_independent_fields_can_move_without_changing_attribution(
    protected, label, template, expected
):
    names = render_names(**{**ARGS, "protected_base": protected}, template=template)
    assert names == (
        expected.format(label=label),
        expected.format(label=label) + "-g01",
        expected.format(label=label) + "-g01-sp2",
    )


@pytest.mark.parametrize(
    "template",
    ["{YYYYMMDD}-{provider_drama}-{drama_id}", "前缀-{provider_drama}-{drama_id}"],
)
def test_combined_field_can_also_move(template):
    assert render_names(**ARGS, template=template)[0].endswith("-106001-A7K2")


@pytest.mark.parametrize(
    "template",
    [
        "{drama_name}-{drama_id}",
        "{provider_label}-{drama_id}",
        "{provider_drama}-{drama_name}-{drama_id}",
        "{provider_drama}-{provider_label}-{drama_id}",
        "{drama_name}-{provider_label}-{drama_id}-{drama_name}",
    ],
)
def test_split_fields_require_both_and_cannot_mix_with_combined(template):
    with pytest.raises(DomainError, match="invalid_name_template"):
        render_names(**ARGS, template=template)
