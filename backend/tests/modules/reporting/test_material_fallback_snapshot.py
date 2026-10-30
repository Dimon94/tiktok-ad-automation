"""旧类型帖子保留金额；使用位置只由真实目录歧义决定。"""

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.query_helpers import query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


@pytest.mark.parametrize("ambiguous", [False, True])
def test_shared_fallback_usage_only_rejects_real_directory_ambiguity(
    session, sync_seed, ambiguous
):
    rows = [material_row("p1", "3"), material_row("p2", "7")]
    for row in rows:
        row["dimensions"]["main_material_type"] = "VIDEO"
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
    session.execute(
        text(
            "INSERT INTO material_post_link (tenant_id,advertiser_id,main_material_id,ad_remote_id,main_material_type,ad_material_id,material_name,platform_material_id,observed_at) SELECT tenant_id,advertiser_id,'p2',ad_remote_id,main_material_type,ad_material_id,material_name,platform_material_id,observed_at FROM material_post_link WHERE tenant_id=:tenant"
        ),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(
        text(
            "UPDATE material_post_link SET main_material_type='VIDEO' WHERE tenant_id=:tenant"
        ),
        {"tenant": sync_seed.tenant_id},
    )
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
        '["post", "acc-1", "p2"]',
        '["post", "acc-1", "p1"]',
    ]
    for row in page.items:
        assert [(u.ad_ref.remote_id, u.ad_material_id) for u in row.material_uses] == (
            [] if ambiguous else [("a1", "m1")]
        )
    assert sum(Decimal(b["values"]["spend"]) for b in page.summary["buckets"]) == 10
