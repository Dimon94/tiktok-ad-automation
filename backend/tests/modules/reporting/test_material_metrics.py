"""JB36 independent arithmetic expectations at the existing PG query seam."""

from decimal import Decimal

import pytest

from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.query_helpers import query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


@pytest.mark.parametrize(
    ("second_spend", "revenues", "expected", "state"),
    [
        ("8", ("1", "8"), "0.9", "AVAILABLE"),
        ("8", ("1", None), "0.1", "PARTIAL"),
        ("8", (None, None), None, "MISSING"),
        (None, ("1", "8"), None, "MISSING"),
        ("0", ("1", "8"), None, "MISSING"),
    ],
)
def test_complete_denominator(
    session, sync_seed, second_spend, revenues, expected, state
):
    from app.modules.ads.creative_models import AdCreative

    first_spend = "0" if second_spend == "0" else "2"
    rows = [material_row("c1", first_spend), material_row("c2", second_spend)]
    for row, revenue in zip(rows, revenues, strict=True):
        row["metrics"]["native_growth_total_ad_impression_value"] = revenue
    publish(session, sync_seed, "acc-1", rows)
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="c1",
    )
    original = session.get(AdCreative, (sync_seed.tenant_id, "acc-1", "c1"))
    session.add(AdCreative(**(original.model_dump() | {"creative_id": "c2"})))
    session.flush()
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            ids=("vid1",),
        ),
    )
    row = page.items[0]
    instance = row.material_instances[0]
    for vector in (instance.metric_buckets[0], row.metric_buckets[0]):
        assert vector.values["roas_total"] == (Decimal(expected) if expected else None)
        assert vector.availability["roas_total"] == state
        assert vector.level == "creative"
    assert row.reconciliation_rows
    assert all(r["status"] == "NOT_COMPARABLE" for r in row.reconciliation_rows)
    assert all(not r["metric_buckets"] for r in row.reconciliation_rows)


def test_independent_full_parent_and_subset(session, sync_seed):
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    publish(
        session, sync_seed, "acc-1", [material_row("c1", "2"), material_row("c2", "8")]
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="c1",
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid2",
        ad_material="mid2",
        post="c2",
    )
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=D,
        end=D,
        rows=[ad_row("ad1", D, spend="10")],
    )

    def query(ids):
        return query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension="material",
                start_date=D,
                end_date=D,
                advertiser_ids=("acc-1",),
                ids=ids,
            ),
        )

    full = query(())
    differences = [
        d
        for r in full.items
        for d in r.reconciliation_rows
        if d["scope"] == "FULL_PARENT_AD"
    ]
    assert differences
    assert differences[0]["status"] == "COMPARABLE", differences[0]["reason"]
    assert differences[0]["metric_buckets"][0]["values"]["spend"] == "0"
    assert differences[0]["metric_buckets"][0]["values"]["revenue_total"] is None
    subset = query(("vid1",))
    control = subset.items[0].parent_controls[0]
    assert control.metric_buckets[0].values["spend"] == 10
    difference = subset.items[0].reconciliation_rows[0]
    assert difference["status"] == "NOT_COMPARABLE"
    assert "SELECTED_MATERIAL_SUBSET" in difference["reason"]
    assert difference["metric_buckets"] == []
    # 对照重复在不同素材的展开里，但导出只能一次；不进入素材汇总。
    assert sum(Decimal(v["values"]["spend"]) for v in full.summary["buckets"]) == 10


def test_currency_timezone_and_parent_not_ready(session, sync_seed):
    from sqlalchemy import text

    publish(session, sync_seed, "acc-1", [material_row("c1", "2")])
    publish(session, sync_seed, "acc-2", [material_row("c2", "8")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad1",
        video="vid1",
        ad_material="mid1",
        post="c1",
    )
    use(
        session,
        sync_seed,
        adv="acc-2",
        ad="ad2",
        video="vid2",
        ad_material="mid2",
        post="c2",
    )
    session.execute(
        text(
            "UPDATE creative_report_day SET currency='EUR', timezone='America/New_York' WHERE tenant_id=:t AND creative_id='c2'"
        ),
        {"t": sync_seed.tenant_id},
    )
    session.execute(
        text(
            "UPDATE report_sync_state SET status='FAILED' WHERE tenant_id=:t AND report_kind='ad_day'"
        ),
        {"t": sync_seed.tenant_id},
    )
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1", "acc-2"),
        ),
    )
    buckets = page.summary["buckets"]
    assert {(v["currency"], v["timezone"]) for v in buckets} == {
        ("USD", "Asia/Hong_Kong"),
        ("EUR", "America/New_York"),
    }
    assert all(
        d["status"] == "NOT_COMPARABLE"
        for r in page.items
        for d in r.reconciliation_rows
    )
    assert any(
        "PARENT_NOT_READY" in d["reason"]
        for r in page.items
        for d in r.reconciliation_rows
    )
    # 相同币种跨本地时区允许查询和求和，逐事实日期/时区仍在冻结证据内。
    from app.modules.reporting.dimension_query import aggregate_metrics
    from app.modules.reporting.schemas import MetricVector

    merged = aggregate_metrics(
        [MetricVector.model_validate(v | {"currency": "USD"}) for v in buckets],
        strict_denominators=True,
    )
    assert merged.timezone == "MIXED"
    assert merged.values["spend"] == 10
