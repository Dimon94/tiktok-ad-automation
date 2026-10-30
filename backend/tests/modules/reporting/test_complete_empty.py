from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.reporting.publish import (
    AccountMeta,
    publish_ad_day,
    publish_material_day,
)
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import METRIC_COLUMNS
from tests.modules.reporting.query_helpers import build_rows
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import D


def seed_empty(session, seed, *, d0="5"):
    for campaign in ("c1", "c2"):
        session.add(
            AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                kind="campaign",
                remote_id=campaign,
                ad_type="SMART_PLUS",
                name="嘉书-同剧",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.add(
            CampaignNameProjection(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                campaign_remote_id=campaign,
                name_revision=1,
                raw_name="嘉书-同剧",
                provider_label="嘉书",
                drama_name="同剧",
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
            )
        )
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[ad_row("a", D, campaign="c1", spend="10", d0=d0)],
    )


def rows(session, seed, dimension="campaign", start=D):
    return build_rows(
        session,
        context=seed.context,
        bc_id="bc-a",
        # 本文件验证目录内无数据对象的补 0/空值口径，需显式列出无数据对象。
        filters=ReportingFilter(
            dimension=dimension,
            start_date=start,
            end_date=D,
            advertiser_ids=("acc-1",),
            include_empty=True,
        ),
    )


@pytest.mark.parametrize(
    "d0,expected,state",
    [("5", Decimal("5"), "AVAILABLE"), ("-", Decimal("0"), "PARTIAL")],
)
def test_complete_empty_campaign_does_not_poison_drama_and_platform_null_is_partial(
    session, sync_seed, d0, expected, state
):
    # 部分合计规则：平台显式 NULL 的成员不参与求和也不当成 0，剧行合计其余成员
    # （此处为确认零消耗的 c2）并标 PARTIAL。
    seed_empty(session, sync_seed, d0=d0)
    result = rows(session, sync_seed, "drama")
    assert len(result) == 1
    vector = result[0].metric_buckets[0]
    assert vector.values["spend"] == Decimal("10")
    assert vector.availability["spend"] == "AVAILABLE"
    assert vector.values["revenue_d0"] == expected
    assert vector.availability["revenue_d0"] == state
    assert vector.values["roas_d0"] == expected / Decimal("10")
    assert vector.availability["roas_d0"] == state
    assert result[0].coverage["status"] == "COMPLETE"


def test_complete_empty_object_has_zero_additive_metrics_and_null_ratios(
    session, sync_seed
):
    seed_empty(session, sync_seed)
    empty = next(r for r in rows(session, sync_seed) if r.display["remote_id"] == "c2")
    vector = empty.metric_buckets[0]
    assert all(vector.values[k] == 0 for k in METRIC_COLUMNS)
    assert all(
        vector.values[k] is None
        for k in (
            "roas_d0",
            "roas_d3",
            "roas_d7",
            "roas_d14",
            "roas_d30",
            "roas_total",
            "ctr",
            "cost_per_ad_impression_event",
        )
    )


def test_incomplete_empty_remains_null_but_existing_facts_keep_sum(session, sync_seed):
    seed_empty(session, sync_seed)
    result = {
        r.display["remote_id"]: r
        for r in rows(session, sync_seed, start=D - timedelta(days=1))
    }
    assert all(result["c2"].metric_buckets[0].values[k] is None for k in METRIC_COLUMNS)
    assert result["c1"].metric_buckets[0].values["spend"] == 10
    assert all(r.coverage["status"] == "INCOMPLETE" for r in result.values())
    assert result["c2"].coverage["stale_dates"] == [str(D - timedelta(days=1))]


@pytest.mark.parametrize("complete", [True, False])
def test_empty_directory_material_uses_own_coverage_and_unsupported_metrics(
    session, sync_seed, complete
):
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="a",
            platform_material_id="p",
            main_material_id="m",
            main_material_type="VIDEO",
            material_type="VIDEO",
            complete=True,
            published_version=1,
        )
    )
    if complete:
        publish_material_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=D,
            end=D,
            rows=[],
        )
    session.flush()
    result = rows(session, sync_seed, "material")
    assert len(result) == 1
    vector = result[0].metric_buckets[0]
    for metric in METRIC_COLUMNS:
        if metric in ("revenue_d3", "revenue_d30"):
            assert (
                vector.values[metric] is None
                and vector.availability[metric] == "UNSUPPORTED"
            )
        else:
            assert vector.values[metric] == (Decimal(0) if complete else None)
    assert vector.values["roas_d0"] is None
    assert result[0].coverage["status"] == ("COMPLETE" if complete else "MISSING")


def test_drama_row_partial_when_member_account_incomplete(session, sync_seed):
    # 账户 A 覆盖完整有消耗，账户 B 未同步且无事实：剧行显示 A 的已同步部分并标 PARTIAL。
    for adv, campaign in (("acc-1", "c1"), ("acc-2", "c9")):
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                kind="campaign",
                remote_id=campaign,
                ad_type="SMART_PLUS",
                name="嘉书-同剧",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=campaign,
                name_revision=1,
                raw_name="嘉书-同剧",
                provider_label="嘉书",
                drama_name="同剧",
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
            )
        )
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[ad_row("a", D, campaign="c1", spend="10", d0="5")],
    )
    result = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="drama",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1", "acc-2"),
            include_empty=True,
        ),
    )
    assert len(result) == 1
    (vector,) = result[0].metric_buckets
    assert vector.values["spend"] == Decimal("10")
    assert vector.availability["spend"] == "PARTIAL"
    assert vector.values["roas_d0"] == Decimal("0.5")
    assert vector.availability["roas_d0"] == "PARTIAL"
    assert result[0].coverage["status"] != "COMPLETE"
    assert result[0].coverage["incomplete_advertiser_ids"] == ["acc-2"]


def test_drama_ids_filter_matches_member_campaign(session, sync_seed):
    # 剧行按成员系列 ID 筛选（成员 ID 数组为 varchar，需与文本数组比较）。
    seed_empty(session, sync_seed)
    result = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="drama",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            ids=("c2",),
            include_empty=True,
        ),
    )
    assert len(result) == 1
    assert {ref.remote_id for ref in result[0].refs} == {"c1", "c2"}


def seed_hierarchy(session, seed):
    """两个同剧系列，各一个广告组和广告；只有 c1/g1/a 有当天消耗。"""
    seed_empty(session, seed)
    session.add_all(
        [
            AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                kind=kind,
                remote_id=rid,
                parent_kind=parent_kind,
                parent_remote_id=parent,
                ad_type="SMART_PLUS",
                name=name,
                observed_at=datetime.now(UTC),
                published_version=1,
            )
            for kind, rid, parent_kind, parent, name in (
                ("adgroup", "g1", "campaign", "c1", "组一"),
                ("adgroup", "g2", "campaign", "c2", "组二"),
                ("ad", "a", "adgroup", "g1", "广告一"),
                ("ad", "a2", "adgroup", "g2", "广告二"),
            )
        ]
    )
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[ad_row("a", D, campaign="c1", adgroup="g1", spend="10", d0="5")],
    )
    session.flush()


@pytest.mark.parametrize("dimension", ["campaign", "drama", "adgroup", "ad"])
def test_default_lists_only_objects_with_facts(session, sync_seed, dimension):
    """默认只列所选日期有数据的对象；勾选显示无数据对象后才补出目录里的无数据对象。"""
    seed_hierarchy(session, sync_seed)

    def query(include_empty):
        return build_rows(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension=dimension,
                start_date=D,
                end_date=D,
                advertiser_ids=("acc-1",),
                include_empty=include_empty,
            ),
        )

    only = query(False)
    assert len(only) == 1
    assert only[0].metric_buckets[0].values["spend"] == Decimal("10")
    # 只按身份读取目录时，上级名称仍来自目录对象。
    if dimension in ("adgroup", "ad"):
        assert only[0].display["campaign_name"] == "嘉书-同剧"
    if dimension == "ad":
        assert only[0].display["adgroup_name"] == "组一"
        assert only[0].display["name"] == "广告一"
    full = query(True)
    # 剧维度两个系列同剧，始终一行；其他维度补出无数据对象。
    assert len(full) == (1 if dimension == "drama" else 2)


def test_default_material_lists_only_videos_with_facts(session, sync_seed):
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="a",
            platform_material_id="p",
            main_material_id="m",
            main_material_type="VIDEO",
            material_type="VIDEO",
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    filters = ReportingFilter(
        dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    assert (
        build_rows(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
        == ()
    )


@pytest.mark.parametrize(
    ("dimension", "changes", "expected"),
    [
        ("campaign", {"query": "c2"}, {"c2"}),
        ("campaign", {"query": "嘉书"}, {"c1", "c2"}),
        ("adgroup", {"ids": ("c2",)}, {"g2"}),
        ("ad", {"ids": ("g2",)}, {"a2"}),
        ("ad", {"query": "广告二"}, {"a2"}),
    ],
)
def test_search_includes_matching_objects_without_facts(
    session, sync_seed, dimension, changes, expected
):
    """默认只列有数据对象，但按名称/ID 搜索或下钻时能找到当天无数据的对象。"""
    seed_hierarchy(session, sync_seed)
    result = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension=dimension,
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            **changes,
        ),
    )
    assert {row.display["remote_id"] for row in result} == expected


@pytest.mark.parametrize("dimension", ["campaign", "adgroup", "ad"])
@pytest.mark.parametrize("sort_by", ["spend", "row_key"])
def test_metrics_only_path_matches_full_directory_path(
    session, sync_seed, dimension, sort_by
):
    """快速路径不读目录，但成员、顺序与展示和读取目录的完整路径一致。"""
    from app.modules.reporting import row_set

    seed_hierarchy(session, sync_seed)
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[
            ad_row("a", D, campaign="c1", adgroup="g1", spend="10", d0="5"),
            ad_row("a2", D, campaign="c2", adgroup="g2", spend="30", d0="5"),
        ],
    )
    session.flush()
    filters = ReportingFilter(
        dimension=dimension,
        start_date=D,
        end_date=D,
        advertiser_ids=("acc-1",),
        sort_by=sort_by,
        sort_direction="desc",
    )
    assert row_set._metrics_only(filters)

    def snapshot():
        return [
            (row.row_key, row.display, row.refs)
            for row in build_rows(
                session, context=sync_seed.context, bc_id="bc-a", filters=filters
            )
        ]

    fast = snapshot()
    original = row_set._metrics_only
    row_set._metrics_only = lambda _filters: False
    try:
        full = snapshot()
    finally:
        row_set._metrics_only = original
    assert fast == full
    assert len(fast) == 2


def test_search_by_full_platform_id_finds_object_without_facts(session, sync_seed):
    seed_hierarchy(session, sync_seed)
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="ad",
            remote_id="1878123303399042",
            parent_kind="adgroup",
            parent_remote_id="g2",
            ad_type="SMART_PLUS",
            name="长ID广告",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.flush()
    result = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="ad", start_date=D, end_date=D, query="1878123303399042"
        ),
    )
    assert [row.display["remote_id"] for row in result] == ["1878123303399042"]
