from datetime import timedelta
from decimal import Decimal

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.modules.ad_management.models import ManagementPreviewItem
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.schemas import MutationSpec
from app.modules.ads.models import AdObject, CampaignNameProjection
from tests.modules.ad_management.conftest import _row, _set_selection


def test_percentage_and_budget_owner_previews(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "series-1")
    group_a = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-a")
    group_b = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-b")
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True, "budget": "100"}, connection_id=route.connection_id),
        _row(group_a, parent=campaign, configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20", "budget_mode": "CAMPAIGN"}, connection_id=route.connection_id),
        _row(group_b, parent=campaign, configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20", "budget_mode": "CAMPAIGN"}, connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [group_a])
    p = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="increase_percent", value=10))
    assert (p.expires_at - p.created_at).total_seconds() == 300
    assert p.items[0].original_value == Decimal("1.20")
    assert p.items[0].final_value == Decimal("1.32")
    _set_selection(selection, [group_a, group_b])
    q = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="budget", mode="set", value=Decimal("120")))
    assert q.counts.selected == 2 and q.counts.targets == 1
    assert q.items[0].ref.kind == "campaign"


def test_ordinary_material_without_ad_reference_is_unsupported(session, management_env):
    context, bc, account, route, selection = management_env
    ad = EntityRef(context.tenant_id, account.advertiser_id, "ad", "ad-ordinary")
    use = MaterialUseRef(ad, "platform-vid", None, "VIDEO")
    session.add(_row(ad, ad_type="REGULAR", configuration={"budget": "10"}, connection_id=route.connection_id))
    session.flush()
    _set_selection(selection, [ad], [use])
    p = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="status", mode="set", value="DISABLE"))
    assert p.items[0].material_use.ad_material_id is None
    assert p.items[0].execution_result == "UNSUPPORTED"
    assert p.counts.targets == 0


def test_same_series_divergent_roas_rejected(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "series-2")
    a = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-c")
    b = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-d")
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True}, connection_id=route.connection_id),
        _row(a, parent=campaign, configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id),
        _row(b, parent=campaign, configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.30"}, connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [a, b])
    with pytest.raises(DomainError, match="最终 ROAS") as error:
        prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="increase_percent", value=10))
    assert error.value.code == "management_roas_series_conflict"


def test_source_fence_and_grouping_revision_are_frozen(session, management_env):
    context, bc, account, route, selection = management_env
    from app.modules.accounts.models import TikTokConnection

    wrong_connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(wrong_connection)
    session.flush()
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "fenced-series")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "fenced-group")
    session.add_all([
        _row(campaign, ad_type="SMART_PLUS", configuration={"budget_optimize_on": True, "budget": "10"}, connection_id=route.connection_id),
        _row(group, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=wrong_connection.id),
        CampaignNameProjection(
            tenant_id=context.tenant_id,
            advertiser_id=account.advertiser_id,
            campaign_remote_id=campaign.remote_id,
            name_revision=7,
            raw_name="provider-drama-note",
            provider_label="provider",
            drama_name="drama",
            status="VALID",
            parser_revision=1,
            grouping_revision=7,
        ),
    ])
    session.flush()
    _set_selection(selection, [group])
    with pytest.raises(DomainError, match="来源"):
        prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="set", value=Decimal("1.30")))
    group_row = session.get(AdObject, (context.tenant_id, account.advertiser_id, "adgroup", group.remote_id))
    assert group_row is not None
    group_row.source_connection_id = route.connection_id
    session.flush()
    p = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="set", value=Decimal("1.30")))
    assert p.items[0].ref == group
    persisted = session.exec(select(ManagementPreviewItem).where(ManagementPreviewItem.preview_id == p.preview_id)).first()
    assert persisted is not None and persisted.grouping_revision == 7


def test_child_inherits_grouping_revision_from_frozen_route_ancestor(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "revision-series")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "revision-group")
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True}, connection_id=route.connection_id, observed_at=selection.created_at - timedelta(minutes=1)),
        _row(group, parent=campaign, configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id, observed_at=selection.created_at - timedelta(minutes=1)),
        CampaignNameProjection(
            tenant_id=context.tenant_id,
            advertiser_id=account.advertiser_id,
            campaign_remote_id=campaign.remote_id,
            name_revision=7,
            raw_name="provider-drama-note",
            provider_label="provider",
            drama_name="drama",
            status="VALID",
            parser_revision=1,
            grouping_revision=7,
        ),
    ])
    session.flush()
    _set_selection(selection, [group])
    preview = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="roas", mode="set", value=Decimal("1.30")),
    )
    assert preview.items[0].execution_result == "PENDING"
    assert preview.items[0].ref == group
    persisted = session.exec(select(ManagementPreviewItem).where(ManagementPreviewItem.preview_id == preview.preview_id)).first()
    assert persisted is not None and persisted.grouping_revision == 7


def test_smart_plus_series_expands_only_preselection_siblings_and_conflicts(session, management_env):
    context, bc, account, route, selection = management_env
    cutoff = selection.created_at
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "smart-series")
    selected = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "selected-group")
    sibling = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "sibling-group")
    late = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "late-group")
    old = cutoff - timedelta(minutes=1)
    future = cutoff + timedelta(minutes=1)
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True}, ad_type="SMART_PLUS", connection_id=route.connection_id, observed_at=old),
        _row(selected, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id, observed_at=old),
        _row(sibling, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id, observed_at=old),
        _row(late, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "9.00"}, connection_id=route.connection_id, observed_at=future),
    ])
    session.flush()
    _set_selection(selection, [selected])
    p = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="set", value=Decimal("1.50")))
    assert {item.ref.remote_id for item in p.items if item.execution_result == "PENDING"} == {selected.remote_id, sibling.remote_id}
    assert late.remote_id not in {item.ref.remote_id for item in p.items}
    # A divergent historical sibling is a fail-closed series conflict.
    sibling_row = session.get(AdObject, (context.tenant_id, account.advertiser_id, "adgroup", sibling.remote_id))
    assert sibling_row is not None
    sibling_row.configuration = {"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.80"}
    session.flush()
    _set_selection(selection, [selected])
    with pytest.raises(DomainError, match="最终 ROAS") as error:
        prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="increase_percent", value=10))
    assert error.value.code == "management_roas_series_conflict"


def test_campaign_selection_expands_existing_smart_plus_groups(session, management_env):
    context, bc, account, route, selection = management_env
    cutoff = selection.created_at
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "campaign-selection")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "campaign-smart-group")
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True}, ad_type="REGULAR", connection_id=route.connection_id, observed_at=cutoff - timedelta(minutes=1)),
        _row(group, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id, observed_at=cutoff - timedelta(minutes=1)),
    ])
    session.flush()
    _set_selection(selection, [campaign])
    preview = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="roas", mode="set", value=Decimal("1.50")),
    )
    assert {item.ref.remote_id for item in preview.items if item.execution_result == "PENDING"} == {
        group.remote_id
    }


def test_account_selection_fallback_expands_route_fenced_series(session, management_env):
    context, bc, account, route, selection = management_env
    old = selection.created_at - timedelta(minutes=1)
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "account-series")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "account-group")
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True}, ad_type="REGULAR", connection_id=route.connection_id, observed_at=old),
        _row(group, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id, observed_at=old),
    ])
    session.flush()
    # Account rows can be aggregate B selections with no synthetic account ref.
    selection.filters = {"dimension": "account"}
    _set_selection(selection, [])
    preview = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="roas", mode="set", value=Decimal("1.50")),
    )
    assert preview.counts.selected == 1
    assert {item.ref for item in preview.items if item.execution_result == "PENDING"} == {group}
    assert all(item.ref.kind != "account" for item in preview.items)


def test_excluding_campaign_does_not_fallback_to_full_account_scope(session, management_env):
    context, bc, account, route, selection = management_env
    old = selection.created_at - timedelta(minutes=1)
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "excluded-account-series")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "excluded-account-group")
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True}, connection_id=route.connection_id, observed_at=old),
        _row(group, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id, observed_at=old),
    ])
    session.flush()
    selection.filters = {"dimension": "account"}
    _set_selection(selection, [campaign])
    preview = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(
            field="roas",
            mode="set",
            value=Decimal("1.50"),
            excluded_refs=(campaign,),
        ),
    )
    assert preview.counts.selected == 0
    assert all(item.ref != group for item in preview.items)


def test_account_series_fallback_honors_excluded_campaign_and_group(session, management_env):
    context, bc, account, route, selection = management_env
    old = selection.created_at - timedelta(minutes=1)
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "excluded-series-fallback")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "excluded-group-fallback")
    session.add_all([
        _row(campaign, configuration={"budget_optimize_on": True}, connection_id=route.connection_id, observed_at=old),
        _row(group, parent=campaign, ad_type="SMART_PLUS", configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id, observed_at=old),
    ])
    session.flush()
    selection.filters = {"dimension": "account"}
    _set_selection(selection, [])
    excluded_campaign = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(
            field="roas",
            mode="set",
            value=Decimal("1.50"),
            excluded_refs=(campaign,),
        ),
    )
    assert all(item.execution_result != "PENDING" for item in excluded_campaign.items)
    excluded_group = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(
            field="roas",
            mode="set",
            value=Decimal("1.50"),
            excluded_refs=(group,),
        ),
    )
    assert all(item.execution_result != "PENDING" for item in excluded_group.items)


def test_unrelated_include_parent_and_excluded_parent_are_fenced(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "parent-series")
    selected = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "parent-group")
    unrelated = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "unrelated-series")
    session.add_all([
        _row(campaign, connection_id=route.connection_id),
        _row(selected, parent=campaign, connection_id=route.connection_id),
        _row(unrelated, connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [selected])
    with pytest.raises(DomainError, match="显式父级"):
        prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="status", mode="set", value="DISABLE", include_parents=(unrelated,)))
    p = prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="status", mode="set", value="DISABLE", include_parents=(campaign,), excluded_refs=(campaign,)))
    assert all(item.ref != campaign or item.execution_result == "UNSUPPORTED" for item in p.items)


def test_missing_include_parent_is_retained_as_unsupported(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "existing-parent")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "missing-parent-group")
    missing_parent = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "missing-parent")
    session.add_all([
        _row(campaign, connection_id=route.connection_id),
        _row(group, parent=campaign, connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [group])
    preview = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(
            field="status",
            mode="set",
            value="DISABLE",
            include_parents=(missing_parent,),
        ),
    )
    missing = [item for item in preview.items if item.ref == missing_parent]
    assert len(missing) == 1
    assert missing[0].reason == "parent_not_found"
    assert missing[0].execution_result == "UNSUPPORTED"
    assert preview.counts.targets == 1


def test_missing_material_reference_cannot_fallback_to_ad_status(session, management_env):
    context, bc, account, route, selection = management_env
    ad = EntityRef(context.tenant_id, account.advertiser_id, "ad", "missing-material-reference")
    use = MaterialUseRef(ad, "platform-vid", "ad-material-id", "VIDEO")
    session.add(_row(ad, ad_type="SMART_PLUS", connection_id=route.connection_id))
    session.flush()
    _set_selection(selection, [ad], [use])
    preview = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        MutationSpec(field="status", mode="set", value="DISABLE"),
    )
    assert preview.items[0].reason == "material_reference_missing"
    assert preview.items[0].execution_result == "UNSUPPORTED"
    assert preview.counts.targets == 0


def test_group_budget_campaign_selection_targets_its_adgroups(session, management_env):
    """组预算策略的系列预算固定为 0，选中系列改预算应落到它的广告组。"""
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "group-budget-series")
    group_a = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-budget-a")
    group_b = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-budget-b")
    campaign_config = {"budget": 0, "budget_optimize_on": False, "budget_mode": "BUDGET_MODE_INFINITE"}
    # 只有冻结选择之前已观察到的广告组才属于本次影响范围。
    frozen_before = selection.created_at - timedelta(seconds=1)
    late = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-budget-late")
    session.add_all([
        _row(campaign, configuration=campaign_config, connection_id=route.connection_id),
        _row(group_a, parent=campaign, configuration={"budget": "50"}, connection_id=route.connection_id, observed_at=frozen_before),
        _row(group_b, parent=campaign, configuration={"budget": "80"}, connection_id=route.connection_id, observed_at=frozen_before),
        _row(late, parent=campaign, configuration={"budget": "90"}, connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [campaign])
    preview = prepare_preview(
        session, context, bc.bc_id, selection.id,
        MutationSpec(field="budget", mode="increase_percent", value=Decimal("10")),
    )
    pending = {item.ref.remote_id: item for item in preview.items if item.execution_result == "PENDING"}
    assert set(pending) == {"group-budget-a", "group-budget-b"}
    assert pending["group-budget-a"].final_value == Decimal("55")
    assert pending["group-budget-b"].final_value == Decimal("88")
    assert pending["group-budget-a"].name == "object-group-budget-a"
    selected = [item for item in preview.items if item.ref == campaign]
    assert selected[0].reason == "linked_impact"
    assert preview.counts.targets == 2


def test_adgroup_budget_follows_campaign_budget_strategy(session, management_env):
    """广告组即便返回 budget 字段，所属系列开启 CBO 时预算仍归系列。"""
    context, bc, account, route, selection = management_env
    cbo = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "cbo-series")
    cbo_group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "cbo-group")
    split = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "split-series")
    split_group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "split-group")
    session.add_all([
        _row(cbo, configuration={"budget": "300", "budget_optimize_on": True}, connection_id=route.connection_id),
        _row(cbo_group, parent=cbo, configuration={"budget": "0"}, connection_id=route.connection_id),
        _row(split, configuration={"budget": 0, "budget_optimize_on": False}, connection_id=route.connection_id),
        _row(split_group, parent=split, configuration={"budget": "40"}, connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [cbo_group, split_group])
    preview = prepare_preview(
        session, context, bc.bc_id, selection.id,
        MutationSpec(field="budget", mode="set", value=Decimal("120")),
    )
    targets = {
        (item.ref.kind, item.ref.remote_id)
        for item in preview.items
        if item.execution_result == "PENDING"
    }
    assert targets == {("campaign", "cbo-series"), ("adgroup", "split-group")}


def test_invalid_current_or_final_budget_marks_only_that_item(session, management_env):
    """单个对象的当前值或计算结果非法时只标记该条，其余对象照常预览。"""
    context, bc, account, route, selection = management_env
    zero = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "zero-budget-series")
    small = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "small-budget-series")
    large = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "large-budget-series")
    session.add_all([
        _row(zero, configuration={"budget": "0", "budget_optimize_on": True}, connection_id=route.connection_id),
        _row(small, configuration={"budget": "20", "budget_optimize_on": True}, connection_id=route.connection_id),
        _row(large, configuration={"budget": "200", "budget_optimize_on": True}, connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [zero, small, large])
    preview = prepare_preview(
        session, context, bc.bc_id, selection.id,
        MutationSpec(field="budget", mode="subtract", value=Decimal("50")),
    )
    by_id = {item.ref.remote_id: item for item in preview.items}
    assert by_id["zero-budget-series"].reason == "configuration_invalid"
    assert by_id["small-budget-series"].reason == "final_value_invalid"
    assert by_id["large-budget-series"].execution_result == "PENDING"
    assert by_id["large-budget-series"].final_value == Decimal("150")
    assert preview.counts.targets == 1 and preview.counts.unsupported == 2


def test_enable_reports_disabled_parent_and_includes_it_when_asked(session, management_env):
    """启用下级时暂停的上级只提示；把提示的上级作为 include_parents 传回即一起启用。"""
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "paused-series")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "paused-series-group")
    session.add_all([
        _row(campaign, status="DISABLE", connection_id=route.connection_id),
        _row(group, parent=campaign, status="DISABLE", connection_id=route.connection_id),
    ])
    session.flush()
    _set_selection(selection, [group])
    first = prepare_preview(
        session, context, bc.bc_id, selection.id,
        MutationSpec(field="status", mode="set", value="ENABLE"),
    )
    blocked = [item for item in first.items if item.reason == "parent_disabled"]
    assert [item.ref for item in blocked] == [campaign]
    assert blocked[0].name == "object-paused-series"
    assert first.counts.targets == 1
    second = prepare_preview(
        session, context, bc.bc_id, selection.id,
        MutationSpec(field="status", mode="set", value="ENABLE", include_parents=(campaign,)),
    )
    pending = {item.ref.remote_id for item in second.items if item.execution_result == "PENDING"}
    assert pending == {"paused-series", "paused-series-group"}
    with pytest.raises(DomainError) as error:
        prepare_preview(
            session, context, bc.bc_id, selection.id,
            MutationSpec(field="status", mode="set", value="ENABLE", include_parents=(group,)),
        )
    assert error.value.code == "management_parent_invalid"


def test_bounds_clamp_increase_and_decrease():
    from app.modules.ad_management.expansion import _final_value

    up = MutationSpec(field="budget", mode="increase_percent", value=Decimal("50"), max_value=Decimal("120"))
    assert _final_value(Decimal("100"), up) == Decimal("120")
    down = MutationSpec(field="budget", mode="decrease_percent", value=Decimal("50"), min_value=Decimal("80"))
    assert _final_value(Decimal("100"), down) == Decimal("80")
    # 当前值已达上限时钳制结果等于当前值,预览判为 NO_CHANGE。
    assert _final_value(Decimal("120"), up) == Decimal("120")


@pytest.mark.parametrize("kwargs", [
    {"field": "budget", "mode": "set", "value": "10", "max_value": "20"},
    {"field": "budget", "mode": "add", "value": "10", "min_value": "5"},
    {"field": "status", "mode": "set", "value": "ENABLE", "max_value": "5"},
    {"field": "budget", "mode": "add", "value": "10", "max_value": "0"},
])
def test_bounds_validation(kwargs):
    with pytest.raises(ValueError):
        MutationSpec.model_validate(kwargs)


def test_bounds_never_reverse_direction():
    from app.modules.ad_management.expansion import _final_value

    # 当前值已高于上限时「提高」不能变成降低，结果保持当前值（预览 NO_CHANGE）。
    up = MutationSpec(field="budget", mode="increase_percent", value=Decimal("20"), max_value=Decimal("120"))
    assert _final_value(Decimal("150"), up) == Decimal("150")
    down = MutationSpec(field="budget", mode="subtract", value=Decimal("10"), min_value=Decimal("80"))
    assert _final_value(Decimal("50"), down) == Decimal("50")


def test_disabled_default_connection_asks_to_switch_default(session, management_env):
    """默认执行连接停用时提示切换默认连接，而不是笼统的“资源版本或请求标识冲突”。"""
    from app.core.errors import ERROR_HTTP_STATUS, ERROR_PUBLIC_MESSAGES
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import TikTokConnection

    context, bc, account, route, selection = management_env
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "group-a")
    session.add(_row(group, configuration={"deep_bid_type": "VO_MIN_ROAS", "roas_bid": "1.20"}, connection_id=route.connection_id))
    _set_selection(selection, [group])
    # 生产现场：报表读取仍走可用连接，默认执行连接指向另一个已停用的连接。
    disabled = TikTokConnection(tenant_id=context.tenant_id, status="DISABLED", kind="OFFICIAL_MCP")
    session.add(disabled)
    session.flush()
    session.add(BCConnectionBinding(tenant_id=context.tenant_id, bc_id=bc.bc_id, connection_id=disabled.id, kind=disabled.kind))
    default = session.get(BCDefaultRoute, (context.tenant_id, bc.bc_id))
    default.connection_id = disabled.id
    session.add(default)
    session.flush()
    with pytest.raises(DomainError) as error:
        prepare_preview(session, context, bc.bc_id, selection.id, MutationSpec(field="roas", mode="set", value=Decimal("1.03")))
    assert error.value.code == "bc_default_connection_unavailable"
    assert ERROR_HTTP_STATUS[error.value.code] == 409
    assert "切换" in ERROR_PUBLIC_MESSAGES[error.value.code]
