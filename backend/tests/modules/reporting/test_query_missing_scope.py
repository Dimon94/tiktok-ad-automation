"""PR9 缺失贡献：真实查询及冻结汇总不能只保留有实例的金额。"""

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.modules.reporting.metric_catalog import CREATIVE_OBSERVED_CONTRACT
from app.modules.reporting.schemas import MetricVector, ReportingFilter
from tests.modules.reporting.evidence_helpers import records as resolved_records
from tests.modules.reporting.query_helpers import query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


@pytest.mark.parametrize(
    "same_video,currency", [(True, "USD"), (False, "USD"), (False, "EUR")]
)
def test_missing_material_contribution_survives_frozen_summary(
    session, sync_seed, same_video, currency
):
    fact = material_row("known", "10")
    fact["metrics"]["native_growth_total_ad_impression_value"] = "5"
    publish(session, sync_seed, "acc-1", [fact])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a",
        video="known-video",
        ad_material="m1",
        post="known",
    )
    use(
        session,
        sync_seed,
        adv="acc-2",
        ad="b",
        video="known-video" if same_video else "missing-video",
        ad_material="m2",
    )
    session.execute(
        text(
            "UPDATE advertiser_account SET currency=:currency,timezone='America/New_York' WHERE tenant_id=:t AND advertiser_id='acc-2'"
        ),
        {"t": sync_seed.tenant_id, "currency": currency},
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
            include_empty=True,
        ),
    )
    summary = {
        v["currency"]: MetricVector.model_validate(v) for v in page.summary["buckets"]
    }
    assert summary["USD"].values["spend"] == Decimal("10")
    if currency == "USD":
        assert summary["USD"].availability["spend"] == "PARTIAL"
        assert summary["USD"].values["roas_total"] is None
        assert summary["USD"].availability["roas_total"] == "MISSING"
        assert summary["USD"].timezone == "MIXED"
    else:
        assert summary["EUR"].values["spend"] is None
        assert summary["EUR"].availability["spend"] == "MISSING"
    assert sum(len(r.material_instances) for r in page.items) == 1
    assert all(
        i.creative_ids == ("known",) for r in page.items for i in r.material_instances
    )
    for row in page.items:
        for vector in row.metric_buckets:
            assert (vector.level, vector.source_kind, vector.contract_revision) == (
                "creative",
                "PLATFORM_DIRECT",
                CREATIVE_OBSERVED_CONTRACT,
            )
        if same_video:
            assert len(row.metric_buckets) == 1
            assert row.metric_buckets[0].availability["spend"] == "PARTIAL"
            assert row.metric_buckets[0].values["roas_total"] is None


def test_material_with_no_instances_retains_unknown_bucket(session, sync_seed):
    use(session, sync_seed, adv="acc-1", ad="a", video="missing-video", ad_material="m")
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            include_empty=True,
        ),
    )
    assert len(page.items) == 1
    assert not page.items[0].material_instances
    assert len(page.summary["buckets"]) == 1
    vector = MetricVector.model_validate(page.summary["buckets"][0])
    assert vector.values["spend"] is None
    assert vector.availability["spend"] == "MISSING"
    assert vector.level == "creative"


@pytest.mark.parametrize("child_exists", [False, True])
def test_legacy_parent_without_children_preserves_value_but_child_null_wins(
    report_case, child_exists
):
    from tests.modules.reporting.test_queries import filters_for, query

    case = report_case
    params = {"t": case.context.tenant_id, "c": "legacy", "d": case.fact_date}
    if child_exists:
        case.session.execute(
            text("""INSERT INTO ad_report_day
            (tenant_id,advertiser_id,stat_date,ad_id,campaign_id,adgroup_id,currency,timezone,fetched_at,sync_job_id,spend)
            VALUES (:t,'report-account',:d,'child',:c,'g','USD','UTC',now(),gen_random_uuid(),NULL)"""),
            params,
        )
    # 纯历史父事实没有广告子行；另一个分支刻意留下陈旧父值以检验真实子NULL优先。
    case.session.execute(
        text("""INSERT INTO campaign_report_day
        (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
        VALUES (:t,'report-account',:d,:c,'Legacy',1,'USD','UTC',now(),gen_random_uuid())
        ON CONFLICT (tenant_id,advertiser_id,stat_date,campaign_id) DO UPDATE SET spend=1"""),
        params,
    )
    page = query(case, filters=filters_for("campaign"))
    vector = page.items[0].metric_buckets[0]
    assert vector.values["spend"] == (None if child_exists else Decimal(1))
    assert vector.source_kind == "CHILD_DERIVED"
    assert vector.contract_revision == "LEGACY_UNKNOWN"
    observation = resolved_records(case.session, case, vector)[0]
    assert observation["remote_id"] == ("child" if child_exists else "legacy")
    assert observation["returned"] is True
    assert observation["publication"] == {}


def test_native_known_and_missing_member_keep_denominator_gap_in_row_and_summary(
    session, sync_seed
):
    from datetime import UTC, datetime

    from app.modules.ads.models import AdObject, CampaignNameProjection
    from tests.modules.reporting.test_native_scope_roas import (
        D as native_day,
    )
    from tests.modules.reporting.test_native_scope_roas import (
        publish as native_publish,
    )

    native_publish(session, sync_seed, ("10",), ("5",))
    for adv, rid in (("acc-1", "c0"), ("acc-2", "missing")):
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                kind="campaign",
                remote_id=rid,
                ad_type="NATIVE_SERIES",
                name="P-Drama",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=rid,
                raw_name="P-Drama",
                provider_label="P",
                drama_name="Drama",
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
                name_revision=1,
            )
        )
    filters = ReportingFilter(
        dimension="drama",
        start_date=native_day,
        end_date=native_day,
        advertiser_ids=("acc-1", "acc-2"),
        include_empty=True,
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert len(page.items) == 1
    for vectors in (
        page.items[0].metric_buckets,
        tuple(MetricVector.model_validate(v) for v in page.summary["buckets"]),
    ):
        assert len(vectors) == 2
        known = next(v for v in vectors if v.source_kind == "PLATFORM_DIRECT")
        assert known.values["spend"] == 10
        assert known.availability["spend"] == "PARTIAL"
        assert known.values["roas_total"] is None
        assert known.availability["roas_total"] == "MISSING"
        assert all(
            o["advertiser_id"] == "acc-1"
            for o in resolved_records(session, sync_seed, known)
        )
    filtered = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"min_d0_roas": Decimal("0.1")}),
    )
    assert filtered.total == 0


def test_partial_window_empty_authority_cannot_make_denominator_zero(
    session, sync_seed
):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.ads.models import AdObject, CampaignNameProjection
    from app.modules.reporting.publish import AccountMeta, publish_campaign_day
    from tests.modules.reporting.test_jobs import _route
    from tests.modules.reporting.test_native_scope_roas import D

    end = D + timedelta(days=1)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "requested_at": datetime.now(UTC),
        "route": FrozenTikTokRoute(**_route(sync_seed)),
    }
    publish_campaign_day(
        session,
        **args,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=end,
        rows=[
            {
                "dimensions": {"campaign_id": rid, "stat_time_day": str(day)},
                "metrics": {
                    "spend": "5",
                    "native_growth_total_ad_impression_value": revenue,
                    "native_growth_ad_revenue_value_d0": revenue,
                },
            }
            for day in (D, end)
            for rid, revenue in (("c0", "2.5"), ("good", "10"))
        ],
    )
    publish_campaign_day(
        session,
        **args,
        job_id=uuid4(),
        accounts={"acc-2": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[],
    )
    for adv, rid, drama in (
        ("acc-1", "c0", "Drama"),
        ("acc-2", "missing", "Drama"),
        ("acc-1", "good", "Other"),
    ):
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                kind="campaign",
                remote_id=rid,
                ad_type="NATIVE_SERIES",
                name=f"P-{drama}",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=rid,
                raw_name=f"P-{drama}",
                provider_label="P",
                drama_name=drama,
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
                name_revision=1,
            )
        )
    filters = ReportingFilter(
        dimension="drama",
        start_date=D,
        end_date=end,
        advertiser_ids=("acc-1", "acc-2"),
        include_empty=True,
        sort_by="roas_d0",
        sort_direction="desc",
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    missing = next(r for r in page.items if r.display["name"] == "Drama")
    for vector, spend in (
        (missing.metric_buckets[0], Decimal("10")),
        (MetricVector.model_validate(page.summary["buckets"][0]), Decimal("20")),
    ):
        assert vector.values["spend"] == spend
        assert vector.availability["spend"] == "PARTIAL"
        assert vector.values["roas_d0"] is None
        assert vector.availability["roas_d0"] == "MISSING"
    assert page.items[-1].row_key == missing.row_key
    filtered = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"min_d0_roas": Decimal("0.1")}),
    )
    assert [r.display["name"] for r in filtered.items] == ["Other"]
