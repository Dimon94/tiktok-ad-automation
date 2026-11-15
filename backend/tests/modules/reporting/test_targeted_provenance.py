"""R1: precise Ad facts retain their own observation, never an account stamp."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.reporting.metric_catalog import NATIVE_CONTRACT
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.queries import query_ads
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_native_scope_roas import D


@pytest.mark.parametrize("prior_full", [True, False])
def test_targeted_observation(session, sync_seed, prior_full):
    t0 = datetime.now(UTC) - timedelta(minutes=2)
    t1 = t0 + timedelta(minutes=1)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "accounts": {"acc-1": AccountMeta("USD", "UTC")},
        "start": D,
        "end": D,
    }
    if prior_full:
        publish_ad_day(
            session,
            **args,
            job_id=uuid4(),
            requested_at=t0,
            rows=[
                ad_row("a1", D, total="20"),
                ad_row("a2", D, campaign="c2", total="90"),
            ],
        )
        session.commit()
        old = session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", "ad_day", D))
        baseline = (old.observed_at, old.publication_version, old.publication_digest)
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t1,
        campaign_ids=("c1",),
        rows=[ad_row("a1", D, spend="50", total="100")],
    )
    session.commit()
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="ad", start_date=D, end_date=D, advertiser_ids=("acc-1",)
        ),
    )
    row = next(r for r in page.items if r.refs[0].remote_id == "a1")
    v = row.metric_buckets[0]
    assert v.values["revenue_total"] == Decimal("100")
    p = v.provenance["revenue_total"]
    assert p.buckets[0].observed_at == t1
    assert p.contract_revision == NATIVE_CONTRACT
    obs = p.observations[0]["publication"]
    assert obs["scope"]["campaign_ids"] == ["c1"]
    assert obs["publication_digest"]
    assert obs["publication_id"]
    if prior_full:
        assert obs["publication_digest"] != baseline[2]
        state = session.get(
            ReportSyncState, (sync_seed.tenant_id, "acc-1", "ad_day", D)
        )
        assert (
            state.observed_at,
            state.publication_version,
            state.publication_digest,
        ) == baseline
        other = next(r for r in page.items if r.refs[0].remote_id == "a2")
        assert (
            other.metric_buckets[0].provenance["revenue_total"].buckets[0].observed_at
            == t0
        )
    else:
        assert (
            session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", "ad_day", D))
            is None
        )


@pytest.mark.parametrize("dimension", ["campaign", "adgroup", "account", "drama"])
@pytest.mark.parametrize("contribution", [False, True])
def test_derived_and_contribution_keep_child_observations(
    session, sync_seed, dimension, contribution
):
    from app.modules.reporting.schemas import AdContributionScope

    t0 = datetime.now(UTC) - timedelta(minutes=2)
    t1 = t0 + timedelta(minutes=1)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "accounts": {"acc-1": AccountMeta("USD", "UTC")},
        "start": D,
        "end": D,
    }
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t0,
        rows=[ad_row("a1", D, total="20"), ad_row("a2", D, total="90")],
    )
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t1,
        campaign_ids=("c1",),
        rows=[ad_row("a1", D, total="100"), ad_row("a2", D, total="90")],
    )
    session.commit()
    filters = ReportingFilter(
        dimension=dimension,
        start_date=D,
        end_date=D,
        advertiser_ids=("acc-1",),
        contribution=AdContributionScope(kind="ad", ids=("a1",))
        if contribution
        else None,
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    vector = page.items[0].metric_buckets[0]
    assert vector.values["revenue_total"] == Decimal("100" if contribution else "190")
    assert vector.contract_revision == NATIVE_CONTRACT
    assert vector.source_kind == (
        "PLATFORM_DIRECT" if contribution else "CHILD_DERIVED"
    )
    assert {p.observed_at for p in vector.provenance["revenue_total"].buckets} == {t1}
    assert {
        p["remote_id"] for p in vector.provenance["revenue_total"].observations
    } == ({"a1"} if contribution else {"a1", "a2"})


def test_snapshot_detail_and_csv_copy_precise_observation(session, sync_seed):
    import csv
    import io
    import json

    from app.modules.ads.models import AdObject
    from app.modules.reporting.detail import get_ad_detail
    from app.modules.reporting.exports import (
        create_export,
        download_export,
        generate_export,
    )
    from app.modules.reporting.query_models import QuerySnapshot, ReportExport

    t1 = datetime.now(UTC)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "accounts": {"acc-1": AccountMeta("USD", "UTC")},
        "start": D,
        "end": D,
    }
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="ad",
            remote_id="a1",
            name="A",
            ad_type="NATIVE_SERIES",
            observed_at=t1,
            published_version=1,
        )
    )
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t1,
        campaign_ids=("c1",),
        rows=[ad_row("a1", D, total="100")],
    )
    session.commit()
    filters = ReportingFilter(
        dimension="ad", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    snap = session.get(QuerySnapshot, page.snapshot.snapshot_id)
    assert any(
        p.get("scope", {}).get("campaign_ids") == ["c1"] for p in snap.publication_map
    )
    export = create_export(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=snap.id,
        idempotency_key="precise",
    )
    saved = session.get(ReportExport, export.id)
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t1 + timedelta(seconds=1),
        campaign_ids=("c1",),
        rows=[ad_row("a1", D, total="900")],
    )
    detail = get_ad_detail(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        ref=page.items[0].refs[0],
        snapshot_id=snap.id,
    )
    assert detail.report_result == page.items[0]
    generate_export(session, saved)
    content = download_export(
        session, context=sync_seed.context, bc_id="bc-a", export_id=saved.id
    )
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    assert json.loads(rows[0]["metric_vector"]) == page.items[0].metric_buckets[
        0
    ].model_dump(mode="json")
    assert json.loads(rows[0]["publication_map"]) == snap.publication_map


def test_account_derived_mixed_observations_and_sql_selection(session, sync_seed):
    from app.modules.reporting.schemas import MetricVector

    t0 = datetime.now(UTC) - timedelta(minutes=2)
    t1 = t0 + timedelta(minutes=1)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "accounts": {"acc-1": AccountMeta("USD", "UTC")},
        "start": D,
        "end": D,
    }
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t0,
        rows=[
            ad_row("a1", D, campaign="c1", spend="10", total="20"),
            ad_row("a2", D, campaign="c2", spend="90", total="90"),
        ],
    )
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t1,
        campaign_ids=("c1",),
        rows=[ad_row("a1", D, campaign="c1", spend="50", d0="100", total="100")],
    )
    session.commit()
    filters = ReportingFilter(
        dimension="account", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    vector = page.items[0].metric_buckets[0]
    assert vector.values["revenue_total"] == Decimal("190")
    assert vector.values["spend"] == Decimal("140")
    assert {p.observed_at for p in vector.provenance["revenue_total"].buckets} == {
        t0,
        t1,
    }
    assert vector.source_kind == "CHILD_DERIVED"
    assert MetricVector.model_validate(page.summary["buckets"][0]).values[
        "revenue_total"
    ] == Decimal("190")
    selected = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="ad",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            min_d0_roas=Decimal("1.5"),
            sort_by="roas_total",
            sort_direction="desc",
        ),
    )
    assert [r.refs[0].remote_id for r in selected.items] == ["a1"]
    assert MetricVector.model_validate(selected.summary["buckets"][0]).values[
        "revenue_total"
    ] == Decimal("100")


@pytest.mark.parametrize("level", ["campaign", "adgroup"])
def test_direct_parent_stays_authoritative_after_target_and_empty(
    session, sync_seed, level
):
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.reporting.publish import publish_parent_day
    from tests.modules.reporting.test_jobs import _route

    t0 = datetime.now(UTC) - timedelta(minutes=2)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "accounts": {"acc-1": AccountMeta("USD", "UTC")},
        "start": D,
        "end": D,
    }
    parent = dict(**args, level=level, route=FrozenTikTokRoute(**_route(sync_seed)))
    publish_parent_day(
        session,
        **parent,
        job_id=uuid4(),
        requested_at=t0,
        rows=[
            {
                "dimensions": {f"{level}_id": "same", "stat_time_day": str(D)},
                "metrics": {
                    "spend": "50",
                    "native_growth_total_ad_impression_value": "40",
                },
            }
        ],
    )
    publish_ad_day(
        session,
        **args,
        job_id=uuid4(),
        requested_at=t0 + timedelta(seconds=1),
        campaign_ids=("same",),
        rows=[ad_row("a1", D, campaign="same", adgroup="same", total="900")],
    )
    session.commit()
    f = ReportingFilter(
        dimension=level, start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=f)
    v = page.items[0].metric_buckets[0]
    assert v.values["revenue_total"] == Decimal("40")
    assert v.source_kind == "PLATFORM_DIRECT"
    assert v.provenance["revenue_total"].buckets[0].observed_at == t0
    publish_parent_day(
        session,
        **parent,
        job_id=uuid4(),
        requested_at=t0 + timedelta(seconds=2),
        rows=[],
    )
    session.commit()
    assert (
        query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=f).items
        == ()
    )
