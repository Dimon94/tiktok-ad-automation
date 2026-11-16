"""未知创意不能由同广告素材引用推断身份；动作只取当前精确目录。"""

from decimal import Decimal

import pytest

from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.query_helpers import query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


@pytest.mark.parametrize("ambiguous", [False, True])
def test_retained_links_do_not_map_unknown_creative_or_grant_actions(
    session, sync_seed, ambiguous
):
    rows = [material_row("p1", "3"), material_row("p2", "7")]
    publish(session, sync_seed, "acc-1", rows)
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    if ambiguous:
        use(session, sync_seed, adv="acc-1", ad="a1", video="v1", ad_material="m2")
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            sort_by="spend",
            sort_direction="desc",
        ),
    )
    assert page.total == 2
    assert [row.row_key for row in page.items] == [
        '["unmapped_creative", "acc-1", "p2"]',
        '["material", "VIDEO", "v1"]',
    ]
    assert not page.items[0].material_uses
    assert [
        (u.ad_ref.remote_id, u.ad_material_id) for u in page.items[1].material_uses
    ] == ([] if ambiguous else [("a1", "m1")])
    assert page.summary["scope"] == "SELECTED_MATERIALS"
    assert sum(Decimal(b["values"]["spend"]) for b in page.summary["buckets"]) == 3
    assert page.items[0].metric_buckets[0].values["spend"] == 7
