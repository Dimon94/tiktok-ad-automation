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


def test_source_contract_buckets_are_not_combined(session, sync_seed):
    """R6: direct day40 and next-day child20 are separate, never60."""
    from datetime import timedelta

    from app.modules.reporting.publish import publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    publish(session, sync_seed, ("50",), ("40",))
    next_day = D + timedelta(days=1)
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=next_day,
        end=next_day,
        rows=[ad_row("a1", next_day, campaign="c0", spend="5", total="20", d0="20")],
    )
    for dimension in ("campaign", "account", "drama"):
        page = query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension=dimension,
                start_date=D,
                end_date=next_day,
                advertiser_ids=("acc-1",),
            ),
        )
        for buckets in (
            page.items[0].metric_buckets,
            tuple(MetricVector.model_validate(b) for b in page.summary["buckets"]),
        ):
            assert sorted(v.values["revenue_total"] for v in buckets) == [
                Decimal("20"),
                Decimal("40"),
            ]
            assert {v.source_kind: v.values["roas_total"] for v in buckets} == {
                "PLATFORM_DIRECT": Decimal(".8"),
                "CHILD_DERIVED": Decimal(4),
            }
            assert {v.source_kind for v in buckets} == {
                "PLATFORM_DIRECT",
                "CHILD_DERIVED",
            }
            assert {v.contract_revision for v in buckets} == {
                "native-growth-day-v1",
            }


def test_explicit_ad_contribution_keeps_cost_five(session, sync_seed):
    """R5: whole Campaign40 cannot be divided by selected Ad cost5."""
    from app.modules.reporting.publish import publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    publish(session, sync_seed, ("50",), ("40",))
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[
            ad_row("a1", D, campaign="c0", spend="5", total="-", d0="-"),
            ad_row("a2", D, campaign="c0", spend="45", total="-", d0="-"),
        ],
    )
    page = query(session, sync_seed, contribution={"kind": "ad", "ids": ("a1",)})
    vector = page.items[0].metric_buckets[0]
    assert vector.values["spend"] == Decimal("5")
    assert vector.values["revenue_total"] is None
    assert vector.values["roas_total"] is None
    assert vector.level == "ad"
    assert (
        page.items[0].display["report_scope_reason"]
        == "AD_SUBSET_PARENT_NOT_APPLICABLE"
    )
    assert MetricVector.model_validate(page.summary["buckets"][0]).values[
        "spend"
    ] == Decimal("5")
    for dimension in ("account", "drama"):
        subset = query(
            session, sync_seed, dimension, contribution={"kind": "ad", "ids": ("a1",)}
        )
        summary = MetricVector.model_validate(subset.summary["buckets"][0])
        assert summary.values["spend"] == Decimal(5)
        assert summary.values["revenue_total"] is None
        assert summary.values["roas_total"] is None
    whole = query(session, sync_seed, ids=("c0",))
    assert whole.items[0].metric_buckets[0].values["revenue_total"] == Decimal("40")


def test_unknown_direct_contract_is_not_interpreted(session, sync_seed):
    from app.modules.reporting.store_models import ReportSyncState

    publish(session, sync_seed, ("50",), ("40",))
    state = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "campaign_day", D)
    )
    state.active_contract = "uninterpretable-v0"
    session.add(state)
    session.commit()
    page = query(session, sync_seed)
    for vector in (
        page.items[0].metric_buckets[0],
        MetricVector.model_validate(page.summary["buckets"][0]),
    ):
        assert vector.values["revenue_total"] is None
        assert vector.reasons["revenue_total"] == "CONTRACT_MISMATCH"
        assert vector.contract_revision == "uninterpretable-v0"
    assert page.items[0].display["platform_roas_total"] is None
    assert query(session, sync_seed, min_d0_roas=Decimal("0")).items == ()


def test_platform_ratio_is_not_averaged_across_days(session, sync_seed):
    from datetime import timedelta

    for offset, ratio in ((0, "0.801"), (1, "0.903")):
        day = D + timedelta(days=offset)
        publish_campaign_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=day,
            end=day,
            requested_at=datetime.now(UTC),
            route=FrozenTikTokRoute(**_route(sync_seed)),
            rows=[
                {
                    "dimensions": {"campaign_id": "c0", "stat_time_day": str(day)},
                    "metrics": {
                        "spend": "50",
                        "native_growth_total_ad_impression_value": "40",
                        "native_growth_ad_revenue_roas": ratio,
                    },
                }
            ],
        )
    single = query(session, sync_seed).items[0]
    assert single.display["platform_roas_total"] == "0.801"
    assert single.metric_buckets[0].values["roas_total"] == Decimal("0.8")
    multiple = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="campaign",
            start_date=D,
            end_date=D + timedelta(days=1),
            advertiser_ids=("acc-1",),
        ),
    ).items[0]
    assert multiple.display["platform_roas_total"] is None
    assert multiple.display["platform_roas_total_reason"] == "NON_ADDITIVE"
    assert "0.801" in multiple.display["platform_roas_observations"]
    assert "0.903" in multiple.display["platform_roas_observations"]
    assert multiple.metric_buckets[0].values["roas_total"] == Decimal("0.8")


def test_selected_plan_and_pagination_evidence(session, sync_seed):
    """Small synthetic PG plan evidence; not a production capacity benchmark."""
    import json
    from time import perf_counter

    from sqlalchemy import text

    from app.modules.reporting.row_set import _ctes, _summary_select

    publish(session, sync_seed, ("10", "90"), ("20", "90"))
    filters = ReportingFilter(
        dimension="campaign",
        start_date=D,
        end_date=D,
        advertiser_ids=("acc-1",),
        sort_by="roas_d0",
        sort_direction="desc",
    )
    ctes, params = _ctes(
        filters, tenant_id=sync_seed.tenant_id, advertisers=("acc-1",), bc_id="bc-a"
    )
    plans = {}
    for name, sql in (
        ("members", f"{ctes} SELECT * FROM ordered ORDER BY seq LIMIT 1"),
        (
            "summary",
            f"{ctes} {_summary_select(frozenset(), scoped=True, individual=True)}",
        ),
    ):
        plan = (
            session.connection()
            .execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params)
            .scalar_one()[0]
        )
        plans[name] = plan
    started = perf_counter()
    first = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters, limit=1
    )
    first_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    second = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        snapshot_id=first.snapshot.snapshot_id,
        cursor=first.next_cursor,
        limit=1,
    )
    page_ms = (perf_counter() - started) * 1000
    assert [first.items[0].refs[0].remote_id, second.items[0].refs[0].remote_id] == [
        "c0",
        "c1",
    ]
    assert first.items[0].metric_buckets[0].values["roas_d0"] == Decimal("2")
    assert MetricVector.model_validate(first.summary["buckets"][0]).values[
        "roas_d0"
    ] == Decimal("1.1")
    print(  # noqa: T201 - synthetic execution-plan evidence
        json.dumps(
            {"first_ms": first_ms, "page_ms": page_ms, "plans": plans}, default=str
        )
    )


def test_aggregate_provenance_has_counts_and_original_observations(session, sync_seed):
    publish(session, sync_seed, ("10", "90"), ("20", "-"))
    for dimension in ("account", "drama"):
        page = query(session, sync_seed, dimension)
        for row in page.items:
            vector = row.metric_buckets[0]
            p = vector.provenance["roas_total"]
            assert len(p.buckets) >= 1
            assert p.denominator_missing_count == 0
            assert p.denominator_known_count == len(p.buckets)
            assert p.known_count + p.missing_count == len(p.buckets)
            assert p.local_ranges
            assert p.observations
        assert page.items[0].display["platform_roas_total"] is None
        summary = MetricVector.model_validate(page.summary["buckets"][0])
        assert summary.provenance["roas_total"].denominator_known_count == 2
        assert summary.provenance["revenue_total"].known_count == 1
        assert summary.provenance["revenue_total"].missing_count == 1


def test_complete_empty_retains_direct_coordinates(session, sync_seed):
    from app.modules.ads.models import AdObject

    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="campaign",
            remote_id="empty",
            name="Empty",
            ad_type="NATIVE_SERIES",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.commit()
    publish_campaign_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "America/Los_Angeles")},
        start=D,
        end=D,
        requested_at=datetime.now(UTC),
        route=FrozenTikTokRoute(**_route(sync_seed)),
        rows=[],
    )
    for dimension in ("campaign", "account", "drama"):
        page = query(session, sync_seed, dimension, include_empty=True)
        assert page.items
        for row in page.items:
            vector = row.metric_buckets[0]
            assert vector.level == "campaign"
            assert vector.source_kind == "PLATFORM_DIRECT"
            assert vector.timezone == "America/Los_Angeles"
            assert vector.contract_revision == "native-growth-day-v1"
            assert vector.values["spend"] == 0
            assert vector.values["revenue_total"] is None
            assert vector.provenance["spend"].buckets
            assert vector.provenance["spend"].reason == "NO_RETURNED_ROW"
        summary = MetricVector.model_validate(page.summary["buckets"][0])
        assert summary.provenance["spend"].field_state_counts == {"NO_RETURNED_ROW": 1}


def test_scaled_ratio_provenance_matches_values(session, sync_seed):
    publish_campaign_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        requested_at=datetime.now(UTC),
        route=FrozenTikTokRoute(**_route(sync_seed)),
        rows=[
            {
                "dimensions": {"campaign_id": "c0", "stat_time_day": str(D)},
                "metrics": {"spend": "5", "impressions": "100", "clicks": "2"},
            }
        ],
    )
    page = query(session, sync_seed)
    for vector in (
        page.items[0].metric_buckets[0],
        MetricVector.model_validate(page.summary["buckets"][0]),
    ):
        assert vector.values["cpm"] == Decimal(50)
        assert (
            vector.provenance["cpm"].formula
            == "SUM(spend) / COMPLETE_SUM(impressions) * 1000"
        )


def test_row_contract_conflict_never_uses_current_publication_contract(
    session, sync_seed
):
    from app.modules.reporting.store_models import ParentReportDay

    publish(session, sync_seed, ("50",), ("40",))
    row = session.get(
        ParentReportDay, (sync_seed.tenant_id, "acc-1", "campaign", "c0", D)
    )
    row.evidence = dict(row.evidence) | {"contract_revision": "unregistered-row-v0"}
    session.add(row)
    session.commit()
    page = query(session, sync_seed)
    v = page.items[0].metric_buckets[0]
    assert v.values["spend"] is None
    assert v.contract_revision == "CONTRACT_MISMATCH"
    assert v.provenance["revenue_total"].reason == "CONTRACT_MISMATCH"


def test_representative_selected_plan(session, sync_seed):
    """2,000 synthetic Campaigns: bounded query shape evidence, not production capacity."""
    import json
    from time import perf_counter

    from sqlalchemy import text

    from app.modules.reporting.row_set import _ctes, _summary_select

    publish(session, sync_seed, ("50",) * 2000, ("40",) * 2000)
    filters = ReportingFilter(
        dimension="campaign",
        start_date=D,
        end_date=D,
        advertiser_ids=("acc-1",),
        min_d0_roas=Decimal(".7"),
        sort_by="roas_d0",
        sort_direction="desc",
    )
    ctes, params = _ctes(
        filters, tenant_id=sync_seed.tenant_id, advertisers=("acc-1",), bc_id="bc-a"
    )
    plans = {}
    for name, sql in (
        ("members", f"{ctes} SELECT * FROM ordered ORDER BY seq LIMIT 25"),
        (
            "summary",
            f"{ctes} {_summary_select(frozenset(), scoped=True, individual=True)}",
        ),
    ):
        plans[name] = (
            session.connection()
            .execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params)
            .scalar_one()[0]
        )
    started = perf_counter()
    first = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters, limit=25
    )
    first_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    second = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        limit=25,
        snapshot_id=first.snapshot.snapshot_id,
        cursor=first.next_cursor,
    )
    next_ms = (perf_counter() - started) * 1000
    from sqlalchemy import func
    from sqlmodel import select

    from app.modules.reporting.query_models import QuerySnapshotRow

    payload_bytes = session.exec(
        select(func.sum(func.pg_column_size(QuerySnapshotRow.frozen_result))).where(
            QuerySnapshotRow.snapshot_id == first.snapshot.snapshot_id
        )
    ).one()
    assert len(first.items) == len(second.items) == 25
    assert not {r.row_key for r in first.items} & {r.row_key for r in second.items}
    assert MetricVector.model_validate(first.summary["buckets"][0]).values[
        "revenue_total"
    ] == Decimal("80000")
    assert all(
        r.metric_buckets[0].values["roas_total"] == Decimal(".8")
        for r in first.items + second.items
    )
    from datetime import timedelta

    from app.modules.ads.models import AdObject
    from app.modules.reporting.detail import get_ad_detail
    from app.modules.reporting.job_tasks import cleanup_store

    ref = first.items[0].refs[0]
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id=ref.advertiser_id,
            kind=ref.kind,
            remote_id=ref.remote_id,
            name="Current directory",
            ad_type="NATIVE_SERIES",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.flush()
    started = perf_counter()
    detail = get_ad_detail(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        ref=ref,
        snapshot_id=first.snapshot.snapshot_id,
    )
    detail_ms = (perf_counter() - started) * 1000
    assert detail.report_result == first.items[0]
    started = perf_counter()
    cursors = {}
    for _ in range(22):
        cleanup_store(
            session,
            now=datetime.now(UTC) + timedelta(minutes=16),
            categories=("snapshots",),
            cursors=cursors,
        )
    cleanup_ms = (perf_counter() - started) * 1000
    remaining = session.exec(
        select(func.count())
        .select_from(QuerySnapshotRow)
        .where(QuerySnapshotRow.snapshot_id == first.snapshot.snapshot_id)
    ).one()
    assert remaining == 0
    print(  # noqa: T201 - synthetic PG plan evidence
        "JB28_REPRESENTATIVE "
        + json.dumps(
            {
                "campaigns": 2000,
                "days": 1,
                "frozen_payload_bytes": payload_bytes,
                "detail_ms": detail_ms,
                "cleanup_ms": cleanup_ms,
                "cleanup_batches": 22,
                "first_ms": first_ms,
                "next_ms": next_ms,
                "plans": plans,
            },
            default=str,
        )
    )  # noqa: T201


def test_historical_empty_does_not_borrow_current_account_coordinates(
    session, sync_seed
):
    from app.modules.ads.models import AdObject
    from app.modules.reporting.store_models import ReportSyncState

    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="campaign",
            remote_id="empty",
            name="Empty",
            ad_type="NATIVE_SERIES",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.commit()
    publish(session, sync_seed, (), ())
    state = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "campaign_day", D)
    )
    state.publication_coordinates = None
    session.add(state)
    session.commit()
    page = query(session, sync_seed, include_empty=True)
    for v in (
        page.items[0].metric_buckets[0],
        MetricVector.model_validate(page.summary["buckets"][0]),
    ):
        assert v.currency == v.timezone == "UNKNOWN"
        assert v.values["spend"] == 0
        assert v.values["revenue_total"] is None
        assert v.source_kind == "PLATFORM_DIRECT"
        assert v.provenance["spend"].reason == "NO_RETURNED_ROW"


FIELD_STATES = (None, "-", "ABSENT", "0", "0.1", "90")


@pytest.mark.parametrize(
    "spend,revenue", [(s, r) for s in FIELD_STATES for r in FIELD_STATES]
)
def test_generated_field_states_pg(session, sync_seed, spend, revenue):
    """独立固定锚点10/20 + 生成第二成员，经过发布/SQL/向量/阈值。"""
    rows = []
    for rid, s, r in (("c0", "10", "20"), ("c1", spend, revenue)):
        metrics = {"campaign_name": rid}
        for field, value in (
            ("spend", s),
            ("native_growth_total_ad_impression_value", r),
            ("native_growth_ad_revenue_value_d0", r),
        ):
            if value != "ABSENT":
                metrics[field] = value
        rows.append(
            {
                "dimensions": {"campaign_id": rid, "stat_time_day": str(D)},
                "metrics": metrics,
            }
        )
    publish_campaign_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "America/Los_Angeles")},
        start=D,
        end=D,
        requested_at=datetime.now(UTC),
        route=FrozenTikTokRoute(**_route(sync_seed)),
        rows=rows,
    )
    missing_s = spend in (None, "-", "ABSENT")
    missing_r = revenue in (None, "-", "ABSENT")
    expected_revenue = Decimal(20) + (Decimal(0) if missing_r else Decimal(revenue))
    expected_spend = Decimal(10) + (Decimal(0) if missing_s else Decimal(spend))
    for dimension in ("campaign", "account", "drama"):
        page = query(session, sync_seed, dimension)
        summary = MetricVector.model_validate(page.summary["buckets"][0])
        assert summary.values["revenue_total"] == expected_revenue
        assert summary.values["spend"] == expected_spend
        assert summary.values["roas_total"] == (
            None if missing_s else expected_revenue / expected_spend
        )
        assert summary.availability["revenue_total"] == (
            "PARTIAL" if missing_r else "AVAILABLE"
        )
        p = summary.provenance["roas_total"]
        assert (p.denominator_known_count, p.denominator_missing_count) == (
            2 - int(missing_s),
            int(missing_s),
        )
        assert {b.stat_date for b in p.buckets} == {D}
        assert summary.timezone == "America/Los_Angeles"
    member = next(
        r for r in query(session, sync_seed).items if r.refs[0].remote_id == "c1"
    )
    assert member.metric_buckets[0].values["revenue_total"] == (
        None if missing_r else Decimal(revenue)
    )
    assert member.metric_buckets[0].values["spend"] == (
        None if missing_s else Decimal(spend)
    )
    d0 = member.metric_buckets[0].provenance["revenue_d0"].metric_contract
    assert (d0.window, d0.click_hours, d0.traffic_scope) == (
        "CLICK_HOURS",
        24,
        "UNCONFIRMED",
    )
    assert (
        member.metric_buckets[0].provenance["revenue_total"].metric_contract.window
        == "TOTAL_CUTOFF_UNKNOWN"
    )
    state = (
        "NULL"
        if revenue is None
        else "DASH"
        if revenue == "-"
        else "ABSENT"
        if revenue == "ABSENT"
        else "VALUE"
    )
    assert member.metric_buckets[0].provenance["revenue_total"].field_state_counts == {
        state: 1
    }
    threshold = query(session, sync_seed, min_d0_roas=Decimal(0))
    ids = {row.refs[0].remote_id for row in threshold.items}
    assert ("c1" in ids) == (not missing_s and not missing_r and Decimal(spend) > 0)
    for direction in ("asc", "desc"):
        sorted_page = query(
            session, sync_seed, sort_by="roas_d0", sort_direction=direction
        )
        if missing_s or missing_r or spend == "0":
            assert sorted_page.items[-1].refs[0].remote_id == "c1"


@pytest.mark.parametrize(
    "dimension,subset",
    tuple(
        (d, ids)
        for d in ("campaign", "adgroup")
        for ids in ((), ("a1",), ("a2",), ("a1", "a2"))
    )
    + (("ad", ()),),
)
def test_generated_directory_scope_matrix(session, sync_seed, dimension, subset):
    """复用真实目录投影/发布；逐属性+组合正反例，不虚构素材父贡献。"""
    from app.modules.ads.models import AdObject
    from app.modules.reporting.publish import publish_ad_day, publish_adgroup_day
    from tests.modules.reporting.sync_conftest import ad_row

    for kind, rid, parent in (
        ("campaign", "c0", None),
        ("adgroup", "g0", "c0"),
        ("ad", "a1", "g0"),
    ):
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                kind=kind,
                remote_id=rid,
                parent_kind=None
                if parent is None
                else "campaign"
                if kind == "adgroup"
                else "adgroup",
                parent_remote_id=parent,
                name="Needle " + rid,
                ad_type="NATIVE_SERIES",
                operation_status="ENABLE",
                review_status="APPROVED",
                configuration={
                    "budget_mode": "BUDGET_MODE_DAY",
                    "budget": "100",
                    "create_time": "2026-11-13 00:00:00",
                },
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
    session.commit()
    publish(session, sync_seed, ("50",), ("40",))
    publish_adgroup_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        requested_at=datetime.now(UTC),
        route=FrozenTikTokRoute(**_route(sync_seed)),
        rows=[
            {
                "dimensions": {"adgroup_id": "g0", "stat_time_day": str(D)},
                "metrics": {
                    "campaign_id": "c0",
                    "spend": "50",
                    "native_growth_total_ad_impression_value": "40",
                    "native_growth_ad_revenue_value_d0": "40",
                },
            }
        ],
    )
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[
            ad_row("a1", D, campaign="c0", adgroup="g0", spend="5", total="-", d0="-"),
            ad_row("a2", D, campaign="c0", adgroup="g0", spend="45", total="-", d0="-"),
        ],
    )
    rid = {"campaign": "c0", "adgroup": "g0", "ad": "a1"}[dimension]
    scope = {"contribution": {"kind": "ad", "ids": subset}} if subset else {}
    common = dict(ids=(rid,), **scope)
    attributes = (
        {"query": "Needle"},
        {"operation_statuses": ("ENABLE",)},
        {"review_statuses": ("APPROVED",)},
        {"budget_modes": ("BUDGET_MODE_DAY",)},
        {"created_from": D, "created_to": D},
        {"ad_types": ("NATIVE_SERIES",)},
        {
            "query": "Needle",
            "operation_statuses": ("ENABLE",),
            "budget_modes": ("BUDGET_MODE_DAY",),
            "created_from": D,
            "created_to": D,
        },
    )
    for attrs in attributes:
        page = query(session, sync_seed, dimension, **common, **attrs)
        assert len(page.items) == 1, (dimension, subset, attrs)
        vector = page.items[0].metric_buckets[0]
        expected_spend = (
            sum((Decimal(5) if i == "a1" else Decimal(45) for i in subset), Decimal(0))
            if subset
            else Decimal(5)
            if dimension == "ad"
            else Decimal(50)
        )
        assert vector.values["spend"] == expected_spend, attrs
        assert (
            MetricVector.model_validate(page.summary["buckets"][0]).values["spend"]
            == expected_spend
        )
        assert vector.values["revenue_total"] == (
            None if subset or dimension == "ad" else Decimal(40)
        )
    threshold_page = query(
        session,
        sync_seed,
        dimension,
        **common,
        operation_statuses=("ENABLE",),
        min_d0_roas=Decimal(".7"),
    )
    assert len(threshold_page.items) == (0 if subset or dimension == "ad" else 1)
    if dimension != "campaign":
        parent = "c0" if dimension == "adgroup" else "g0"
        parent_page = query(
            session,
            sync_seed,
            dimension,
            **(common | {"ids": (parent,), "operation_statuses": ("ENABLE",)}),
        )
        assert [r.refs[0].remote_id for r in parent_page.items] == [rid]
    for attrs in (
        {"query": "NoSuchName"},
        {"operation_statuses": ("DISABLE",)},
        {"budget_modes": ("BUDGET_MODE_TOTAL",)},
        {"created_from": date(2026, 11, 14)},
        {"min_d0_roas": Decimal(9)},
    ):
        assert query(session, sync_seed, dimension, **common, **attrs).items == (), (
            attrs
        )
    if dimension != "ad":
        for aggregate in ("account", "drama"):
            page = query(session, sync_seed, aggregate)
            summary = MetricVector.model_validate(page.summary["buckets"][0])
            assert summary.values["revenue_total"] == Decimal(40)
            assert summary.values["spend"] == Decimal(50)


def test_generated_currency_days_pg(session, sync_seed):
    from datetime import timedelta

    for offset, currency, spend, revenue in (
        (0, "USD", "50", "40"),
        (1, "EUR", "5", "20"),
    ):
        day = D + timedelta(days=offset)
        publish_campaign_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta(currency, "UTC")},
            start=day,
            end=day,
            requested_at=datetime.now(UTC),
            route=FrozenTikTokRoute(**_route(sync_seed)),
            rows=[
                {
                    "dimensions": {"campaign_id": "c0", "stat_time_day": str(day)},
                    "metrics": {
                        "spend": spend,
                        "native_growth_total_ad_impression_value": revenue,
                    },
                }
            ],
        )
    for dimension in ("campaign", "account", "drama"):
        page = query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension=dimension,
                start_date=D,
                end_date=D + timedelta(days=1),
                advertiser_ids=("acc-1",),
            ),
        )
        for vectors in (
            page.items[0].metric_buckets,
            tuple(MetricVector.model_validate(b) for b in page.summary["buckets"]),
        ):
            assert {v.currency: v.values["roas_total"] for v in vectors} == {
                "USD": Decimal(".8"),
                "EUR": Decimal(4),
            }


def test_ad_contribution_rejects_unsupported_dimension():
    with pytest.raises(
        ValueError, match="ad contribution requires a parent reporting dimension"
    ):
        ReportingFilter(
            dimension="ad",
            start_date=D,
            end_date=D,
            contribution={"kind": "ad", "ids": ("a1",)},
        )
