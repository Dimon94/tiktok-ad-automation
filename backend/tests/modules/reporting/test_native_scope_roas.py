"""JB28 independent arithmetic: 20/(10+90)=.2; an incomplete divisor is not 10."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.reporting.publish import AccountMeta, publish_campaign_day
from app.modules.reporting.queries import query_ads
from app.modules.reporting.schemas import MetricVector, ReportingFilter
from tests.modules.reporting.test_jobs import _route

D = date(2026, 11, 13)


def publish(session, seed, spends, revenues):
    publish_campaign_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        requested_at=datetime.now(UTC),
        route=FrozenTikTokRoute(**_route(seed)),
        rows=[
            {
                "dimensions": {"campaign_id": f"c{i}", "stat_time_day": str(D)},
                "metrics": {
                    "spend": s,
                    "campaign_name": f"Campaign {i}",
                    "native_growth_total_ad_impression_value": r,
                    "native_growth_ad_revenue_value_d0": r,
                },
            }
            for i, (s, r) in enumerate(zip(spends, revenues, strict=True))
        ],
    )


def query(session, seed, dimension="campaign", **kwargs):
    return query_ads(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension=dimension,
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            **kwargs,
        ),
    )


@pytest.mark.parametrize(
    "spends,revenues,expected,status",
    [
        (("10", "90"), ("20", "90"), Decimal("1.1"), "AVAILABLE"),
        (("10", "90"), ("20", "-"), Decimal(".2"), "PARTIAL"),
        (("10", "-"), ("20", "90"), None, "MISSING"),
        (("0", "0"), ("20", "90"), None, "MISSING"),
    ],
)
def test_complete_denominator(session, sync_seed, spends, revenues, expected, status):
    publish(session, sync_seed, spends, revenues)
    page = query(session, sync_seed)
    vector = MetricVector.model_validate(page.summary["buckets"][0])
    assert vector.values["roas_total"] == expected
    assert vector.availability["roas_total"] == status


def test_direct_plan_threshold_sort_and_account(session, sync_seed):
    publish(session, sync_seed, ("10", "90"), ("20", "90"))
    page = query(
        session,
        sync_seed,
        min_d0_roas=Decimal("1.5"),
        sort_by="roas_d0",
        sort_direction="desc",
    )
    assert [r.refs[0].remote_id for r in page.items] == ["c0"]
    assert page.items[0].metric_buckets[0].values["roas_d0"] == Decimal("2")
    account_page = query(session, sync_seed, "account")
    account = account_page.items[0].metric_buckets[0]
    summary = MetricVector.model_validate(account_page.summary["buckets"][0])
    assert summary.values["revenue_total"] == Decimal("110")
    assert summary.values["spend"] == Decimal("100")
    assert account.values["revenue_total"] == Decimal("110")
    assert account.values["spend"] == Decimal("100")


def test_missing_denominator_excluded_from_threshold_and_sort(session, sync_seed):
    from datetime import timedelta

    from app.modules.reporting.publish import publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    for day, spend in ((D, "10"), (D + timedelta(days=1), "-")):
        publish_ad_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=day,
            end=day,
            rows=[ad_row("a1", day, spend=spend, d0="20", total="20")],
        )
    filters = ReportingFilter(
        dimension="ad",
        start_date=D,
        end_date=D + timedelta(days=1),
        advertiser_ids=("acc-1",),
        min_d0_roas=Decimal("1"),
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert page.items == ()
