# backend/tests/modules/reporting/test_dimension_query.py
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.query_helpers import build_rows
from tests.modules.reporting.sync_conftest import ad_row

D = date(2026, 10, 5)


def _filters(dimension, **kw):
    return ReportingFilter(dimension=dimension, start_date=D, end_date=D, **kw)


def _seed(session, seed):
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={
            "acc-1": AccountMeta("USD", "Asia/Hong_Kong"),
            "acc-2": AccountMeta("USD", "UTC"),
        },
        start=D,
        end=D,
        rows=[
            ad_row("a1", D, campaign="c1", spend="10.00", d0="5.00", total="6.00"),
            ad_row("a2", D, campaign="c1", spend="5.00", d0="1.00", total="1.00"),
            ad_row(
                "b1", D, campaign="c9", spend="2.00", d0="-", total="2.00", adv="acc-2"
            ),
        ],
    )


def test_levels_add_up_and_roas_is_sum_over_sum(session, sync_seed):
    _seed(session, sync_seed)
    ctx = sync_seed.context
    camp = {
        r.display["remote_id"]: r
        for r in build_rows(
            session, context=ctx, bc_id="bc-a", filters=_filters("campaign")
        )
    }
    assert camp["c1"].metric_buckets[0].values["spend"] == Decimal("15.00")
    assert camp["c1"].metric_buckets[0].values["roas_d0"] == Decimal("6.00") / Decimal(
        "15.00"
    )
    acct = {
        r.display["remote_id"]: r
        for r in build_rows(
            session, context=ctx, bc_id="bc-a", filters=_filters("account")
        )
    }
    assert acct["acc-1"].metric_buckets[0].values["spend"] == Decimal("15.00")
    assert (
        acct["acc-2"].metric_buckets[0].values["revenue_d0"] is None
    )  # 有成员缺失 → 合计为空


def test_shared_account_visible_in_both_bcs(session, sync_seed):
    _seed(session, sync_seed)
    rows_b = build_rows(
        session, context=sync_seed.context, bc_id="bc-b", filters=_filters("account")
    )
    assert [r.display["remote_id"] for r in rows_b] == ["acc-2"]


def test_every_metric_sorts_with_null_last(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=_filters("ad", sort_by="roas_d0", sort_direction="desc"),
    )
    assert [r.display["remote_id"] for r in rows] == ["a1", "a2", "b1"]


def test_missing_sync_state_marks_incomplete(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="account", start_date=date(2026, 10, 4), end_date=D
        ),
    )
    assert all(r.coverage["status"] == "INCOMPLETE" for r in rows)  # 10-04 未同步


def test_cross_day_null_sums_present_days_and_marks_partial_names_fall_back_to_facts(
    session, sync_seed
):
    _seed(session, sync_seed)
    day = date(2026, 10, 6)
    raw = ad_row("a1", day, campaign="c1", spend="3", d0="-", total="0")
    raw["metrics"].update(
        ad_name="广告本名", campaign_name="系列本名", adgroup_name="广告组本名"
    )
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=day,
        end=day,
        rows=[raw],
    )
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(dimension="ad", start_date=D, end_date=day),
    )
    row = next(r for r in rows if r.display["remote_id"] == "a1")
    bucket = row.metric_buckets[0]
    assert bucket.values["spend"] == Decimal(13)
    assert bucket.availability["spend"] == "AVAILABLE"
    # 设计 7.3：同一对象跨日也按部分合计——只合计有值的日期并标 PARTIAL。
    # 10-06 的第 29 天回收为空，10-05 有值：第 30 天回收 = 10-05 的值。
    assert bucket.values["revenue_d30"] == Decimal("5.00")
    assert bucket.availability["revenue_d30"] == "PARTIAL"
    assert bucket.availability["roas_d30"] == "PARTIAL"
    assert bucket.values["revenue_d0"] == Decimal("5.00")
    assert bucket.availability["roas_d0"] == "PARTIAL"
    assert row.display["name"] == "广告本名"
    assert row.display["campaign_name"] == "系列本名"
    assert row.display["adgroup_name"] == "广告组本名"


def test_drama_merges_same_currency_timezones_partial_sums_and_avoids_delimiter_collision(
    session, sync_seed
):
    from app.modules.ads.models import CampaignNameProjection

    _seed(session, sync_seed)
    for adv, campaign, provider, drama in (
        ("acc-1", "c1", "P:A", "D"),
        ("acc-2", "c9", "P", "A:D"),
    ):
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=campaign,
                name_revision=1,
                raw_name="test",
                provider_label=provider,
                drama_name=drama,
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
            )
        )
    session.flush()
    rows = build_rows(
        session, context=sync_seed.context, bc_id="bc-a", filters=_filters("drama")
    )
    assert len(rows) == 2
    for p in session.exec(
        __import__("sqlmodel")
        .select(CampaignNameProjection)
        .where(CampaignNameProjection.tenant_id == sync_seed.tenant_id)
    ).all():
        p.provider_label, p.drama_name = "共同版权方", "共同剧"
    session.flush()
    rows = build_rows(
        session, context=sync_seed.context, bc_id="bc-a", filters=_filters("drama")
    )
    assert len(rows) == 1 and len(rows[0].metric_buckets) == 1
    assert rows[0].metric_buckets[0].timezone == "MIXED"
    assert rows[0].metric_buckets[0].values["spend"] == Decimal(17)
    # 一个成员 D0 回收为空：其余成员照常合计，标 PARTIAL 而不是整列置空。
    assert rows[0].metric_buckets[0].values["revenue_d0"] == Decimal(6)
    assert rows[0].metric_buckets[0].availability["revenue_d0"] == "PARTIAL"
    assert rows[0].metric_buckets[0].availability["spend"] == "AVAILABLE"
    assert rows[0].coverage["status"] == "COMPLETE"


def test_reconciliation_keeps_ad_sum_and_shows_account_original(session, sync_seed):
    from datetime import UTC, datetime

    from app.modules.reporting.store_models import AccountReconciliationDay

    _seed(session, sync_seed)
    session.add(
        AccountReconciliationDay(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            stat_date=D,
            account_spend=Decimal("16"),
            ad_sum_spend=Decimal("15"),
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    rows = build_rows(
        session, context=sync_seed.context, bc_id="bc-a", filters=_filters("account")
    )
    row = next(r for r in rows if r.display["remote_id"] == "acc-1")
    assert row.display["reconciliation"] == "DIFF"
    assert Decimal(row.display["account_report_spend"]) == Decimal(16)
    assert row.metric_buckets[0].values["spend"] == Decimal(15)


def _summary(session, seed, filters):
    from app.modules.reporting.queries import query_ads

    return query_ads(
        session, context=seed.context, bc_id="bc-a", filters=filters
    ).summary


def test_summary_keeps_currencies_separate_and_derives_ratios(session, sync_seed):
    # 同币种跨时区合计并标 MIXED，ROAS 由合计重算；不同币种分桶，不换汇。
    D1 = date(2026, 10, 4)
    for adv, meta, day, spend, d0 in (
        ("acc-2", AccountMeta("USD", "UTC"), D1, "10", "1"),
        ("acc-1", AccountMeta("USD", "Asia/Hong_Kong"), D1, "20", "20"),
        ("acc-2", AccountMeta("EUR", "UTC"), D, "100", "1"),
    ):
        publish_ad_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={adv: meta},
            start=day,
            end=day,
            rows=[
                ad_row(
                    f"{adv}-{day}",
                    day,
                    campaign=f"c-{adv}-{day}",
                    spend=spend,
                    d0=d0,
                    adv=adv,
                )
            ],
        )
    summary = _summary(
        session,
        sync_seed,
        ReportingFilter(dimension="campaign", start_date=D1, end_date=D),
    )
    totals = {v["currency"]: v for v in summary["buckets"]}
    assert set(totals) == {"EUR", "USD"}
    assert totals["USD"]["timezone"] == "MIXED"
    assert Decimal(totals["USD"]["values"]["roas_d0"]) == Decimal("0.7")
    assert Decimal(totals["EUR"]["values"]["spend"]) == Decimal("100")


def test_summary_counts_incomplete_advertisers_and_dates(session, sync_seed):
    # acc-2 所选日期未同步：已同步部分照常相加并标 PARTIAL，给出不完整账户与日期。
    _seed_campaign(session, sync_seed, "acc-2", "c-missing")
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=D,
        end=D,
        rows=[ad_row("a1", D, campaign="c1", spend="10", d0="5")],
    )
    summary = _summary(session, sync_seed, _filters("campaign", include_empty=True))
    assert summary["row_count"] == 2
    assert summary["incomplete_advertiser_count"] == 1
    assert summary["incomplete_dates"] == [D.isoformat()]
    (bucket,) = summary["buckets"]
    assert Decimal(bucket["values"]["spend"]) == Decimal("10")
    assert bucket["availability"]["spend"] == "PARTIAL"
    assert bucket["availability"]["roas_d0"] == "PARTIAL"


def test_summary_complete_rows_stay_available(session, sync_seed):
    _seed(session, sync_seed)
    summary = _summary(session, sync_seed, _filters("campaign"))
    (bucket,) = summary["buckets"]
    assert Decimal(bucket["values"]["spend"]) == Decimal("17")
    assert bucket["availability"]["spend"] == "AVAILABLE"
    assert summary["incomplete_advertiser_count"] == 0
    assert summary["incomplete_dates"] == []


def _seed_campaign(session, seed, adv, campaign):
    from datetime import UTC, datetime

    from app.modules.ads.models import AdObject

    session.add(
        AdObject(
            tenant_id=seed.tenant_id,
            advertiser_id=adv,
            kind="campaign",
            remote_id=campaign,
            ad_type="SMART_PLUS",
            name="嘉书-同剧",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.flush()


def test_cpm_and_cpc_are_recomputed_from_totals(session, sync_seed):
    _seed(session, sync_seed)
    camp = {
        r.display["remote_id"]: r
        for r in build_rows(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=_filters("campaign"),
        )
    }
    values = camp["c1"].metric_buckets[0].values
    # ad_row 每行固定 impressions=10、clicks=1；c1 合计消耗 15、展示 20、点击 2。
    assert values["cpm"] == Decimal("15.00") / Decimal("20") * 1000
    assert values["cpc"] == Decimal("15.00") / Decimal("2")


def test_cpm_sorts_desc(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=_filters("ad", sort_by="cpm", sort_direction="desc"),
    )
    assert [r.display["remote_id"] for r in rows] == ["a1", "a2", "b1"]


def test_account_row_equals_sum_of_its_campaigns(session, sync_seed):
    _seed(session, sync_seed)
    ctx = sync_seed.context
    camps = [
        r
        for r in build_rows(
            session, context=ctx, bc_id="bc-a", filters=_filters("campaign")
        )
        if r.display["advertiser_id"] == "acc-1"
    ]
    acct = next(
        r
        for r in build_rows(
            session, context=ctx, bc_id="bc-a", filters=_filters("account")
        )
        if r.display["remote_id"] == "acc-1"
    )
    for key in ("spend", "impressions", "clicks", "revenue_d0", "revenue_total"):
        assert acct.metric_buckets[0].values[key] == sum(
            c.metric_buckets[0].values[key] for c in camps
        )
    assert "account_status" in acct.display


def test_drama_groups_by_second_segment_only_hides_deleted_empty_and_aggregates_status(
    session, sync_seed
):
    from datetime import UTC, datetime

    from sqlmodel import select

    from app.modules.accounts.models import TikTokConnection
    from app.modules.ads.models import AdObject, CampaignNameProjection

    _seed(session, sync_seed)
    connection = session.exec(
        select(TikTokConnection).where(
            TikTokConnection.tenant_id == sync_seed.tenant_id
        )
    ).first()

    def campaign(adv, remote_id, name, delivery):
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                kind="campaign",
                remote_id=remote_id,
                ad_type="SMART_PLUS",
                name=name,
                operation_status="DISABLE",
                delivery_status=delivery,
                review_status="APPROVED",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=connection.id,
                source_channel="OFFICIAL_API",
            )
        )
        provider, drama = name.split("-")[:2]
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=remote_id,
                name_revision=1,
                raw_name=name,
                provider_label=provider,
                drama_name=drama,
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
            )
        )

    # 第一段写法不同（版权方中文、网眼归因参数）不影响归组。
    campaign("acc-1", "c1", "容量-同一部剧-1", "CAMPAIGN_STATUS_DISABLE")
    campaign("acc-2", "c9", "{b31091/s345647/c1}-同一部剧-2", "CAMPAIGN_STATUS_DISABLE")
    # 已删除且所选日期无数据的探针系列不应作为剧出现。
    campaign("acc-1", "probe-1", "probe-capability-ytf", "CAMPAIGN_STATUS_DELETE")
    session.flush()
    rows = build_rows(
        session, context=sync_seed.context, bc_id="bc-a", filters=_filters("drama")
    )
    assert [row.display["name"] for row in rows] == ["同一部剧"]
    drama = rows[0]
    assert {ref.remote_id for ref in drama.refs} == {"c1", "c9"}
    assert drama.metric_buckets[0].values["spend"] == Decimal(17)
    # 版权方显示系列名第一段，多个不同写法全部列出。
    assert set(drama.display["provider"].split("、")) == {
        "容量",
        "{b31091/s345647/c1}",
    }
    # 全部成员已暂停：剧状态为已暂停，而不是取任意成员的状态。
    assert drama.display["operation_status"] == "DISABLE"
    assert drama.display["enabled_campaign_count"] == "0"
    assert drama.display["campaign_count"] == "2"


def test_search_ids_metric_bounds_and_name_sort(session, sync_seed):
    # 关键词按名称/ID 多词 AND；ID 命中行自身或上级；指标区间按币种桶判断；名称排序空值在后。
    _seed(session, sync_seed)

    def keys(**kw):
        return [
            r.display["remote_id"]
            for r in build_rows(
                session,
                context=sync_seed.context,
                bc_id="bc-a",
                filters=_filters("ad", **kw),
            )
        ]

    assert keys(query="a2") == ["a2"]
    assert keys(query="剧c1 广告", sort_by="row_key") == ["a1", "a2"]
    assert keys(query="剧c1 不存在") == []
    assert keys(ids=("c9",)) == ["b1"]
    assert keys(min_spend=Decimal("5"), sort_by="spend", sort_direction="asc") == [
        "a2",
        "a1",
    ]
    assert keys(max_spend=Decimal("4"), min_d0_roas=Decimal("0")) == []
    assert keys(sort_by="campaign_name", sort_direction="desc") == ["b1", "a1", "a2"]
