import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.reporting.publish import AccountMeta, publish_material_day
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import MaterialPostLink
from tests.modules.reporting.query_helpers import build_rows

D = date(2026, 10, 5)


def material_row(post_id, spend, name=None):
    # Spark 广告的素材日报主素材是帖子 ID，类型 VIDEO_SPARK_ADS。
    return {
        "dimensions": {
            "main_material_id": post_id,
            "main_material_type": "VIDEO_SPARK_ADS",
            "stat_time_day": str(D),
        },
        "metrics": {"spend": spend, "main_material_name": name or post_id},
    }


def publish(session, seed, adv, rows):
    publish_material_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={adv: AccountMeta("USD", "Asia/Hong_Kong")},
        start=D,
        end=D,
        rows=rows,
    )


def use(session, seed, *, adv, ad, video, ad_material, post=None, adgroup="g1"):
    """目录里的一个素材使用位置；给出 post 时写入“帖子 → 广告”对照。"""
    now = datetime.now(UTC)
    session.add(
        AdMaterialReference(
            tenant_id=seed.tenant_id,
            advertiser_id=adv,
            ad_remote_id=ad,
            platform_material_id=video,
            ad_material_id=ad_material,
            material_type="VIDEO",
            name=f"{video}.mp4",
            complete=True,
            published_version=1,
        )
    )
    if post:
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id=adv,
                main_material_id=post,
                ad_remote_id=ad,
                main_material_type="VIDEO_SPARK_ADS",
                ad_material_id=ad_material,
                platform_material_id=video,
                observed_at=now,
            )
        )
    for kind, remote_id, parent, name, status in (
        ("ad", ad, adgroup, f"广告{ad}", "ENABLE"),
        ("adgroup", adgroup, "c1", f"组{adgroup}", "DISABLE"),
    ):
        if session.get(AdObject, (seed.tenant_id, adv, kind, remote_id)) is None:
            session.add(
                AdObject(
                    tenant_id=seed.tenant_id,
                    advertiser_id=adv,
                    kind=kind,
                    remote_id=remote_id,
                    parent_kind="adgroup" if kind == "ad" else "campaign",
                    parent_remote_id=parent,
                    ad_type="SMART_PLUS",
                    name=name,
                    operation_status=status,
                    observed_at=now,
                    published_version=1,
                )
            )
    session.flush()


def query(session, seed, **kw):
    return build_rows(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            **({"include_empty": True} | kw),
        ),
    )


def by_id(rows):
    return {r.display["main_material_id"]: r for r in rows}


def test_page_coverage_does_not_rescan_all_accounts_for_every_row(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting import material_query

    publish(session, sync_seed, "acc-1", [material_row("p1", "10")])
    publish(session, sync_seed, "acc-2", [material_row("p2", "20")])
    original = material_query.projected_states
    visits = []
    sizes = []

    class CountedStates(list):
        def __iter__(self):
            for item in super().__iter__():
                visits.append(1)
                yield item

    def counted(*args, **kwargs):
        states, projections = original(*args, **kwargs)
        sizes.append(len(states))
        return CountedStates(states), projections

    monkeypatch.setattr(material_query, "projected_states", counted)
    rows = query(session, sync_seed)
    assert len(rows) == 2
    assert all(row.coverage["status"] == "COMPLETE" for row in rows)
    # 页补全对全账户状态只允许固定次数遍历，不能随账户数或行数相乘。
    assert sum(visits) <= 3 * sum(sizes)


def test_unlinked_posts_keep_money_as_post_rows(session, sync_seed):
    # 对不上广告的帖子单独成行，金额不丢，仍可排序。
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10"), material_row("p2", "20")],
    )
    rows = query(session, sync_seed, sort_by="spend", sort_direction="desc")
    assert [json.loads(r.row_key) for r in rows] == [
        ["post", "acc-1", "p2"],
        ["post", "acc-1", "p1"],
    ]
    for r in rows:
        assert r.coverage["reason"] == "usage_unknown"
        assert r.material_uses == ()
        assert r.display["account_id"] == "acc-1"
        assert r.metric_buckets[0].availability["revenue_d3"] == "UNSUPPORTED"


def test_same_video_across_ads_and_accounts_is_one_row(session, sync_seed):
    # 同一视频在两个账户、三条广告各有帖子：素材维度一行，金额合计且不重复。
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10", "视频一"), material_row("p2", "5")],
    )
    publish(session, sync_seed, "acc-2", [material_row("p3", "7")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a2",
        video="v1",
        ad_material="m2",
        post="p2",
    )
    use(
        session,
        sync_seed,
        adv="acc-2",
        ad="b1",
        video="v1",
        ad_material="m3",
        post="p3",
    )
    rows = query(session, sync_seed)
    assert [json.loads(r.row_key) for r in rows] == [["material", "v1"]]
    row = rows[0]
    assert row.metric_buckets[0].values["spend"] == Decimal(22)
    assert {
        (u.ad_ref.advertiser_id, u.ad_ref.remote_id) for u in row.material_uses
    } == {
        ("acc-1", "a1"),
        ("acc-1", "a2"),
        ("acc-2", "b1"),
    }
    assert row.display["material_name"] == "v1.mp4"
    assert row.display["account_id"] is None
    assert row.display["account_count"] == "2"


def test_directory_video_without_facts_and_link_by_ad_material(session, sync_seed):
    # 对照必须按广告内素材 ID 精确对上：同一广告里的另一个视频不能吃掉这笔钱。
    publish(session, sync_seed, "acc-1", [material_row("p1", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    use(session, sync_seed, adv="acc-1", ad="a1", video="v2", ad_material="m2")
    rows = by_id(query(session, sync_seed))
    assert rows["v1"].metric_buckets[0].values["spend"] == Decimal(10)
    # acc-1 当天素材日报已完整，没有数据的视频填 0。
    assert rows["v2"].metric_buckets[0].values["spend"] == Decimal(0)
    assert [u.ad_material_id for u in rows["v2"].material_uses] == ["m2"]


def test_material_search_and_entity_filter_follow_usages(session, sync_seed):
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10"), material_row("p2", "20")],
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a2",
        video="v2",
        ad_material="m2",
        post="p2",
        adgroup="g2",
    )
    assert [
        r.display["main_material_id"] for r in query(session, sync_seed, query="广告a2")
    ] == ["v2"]
    assert [
        r.display["main_material_id"] for r in query(session, sync_seed, ids=("g1",))
    ] == ["v1"]
    # 帖子 ID 也能搜到所属视频。
    assert [
        r.display["main_material_id"] for r in query(session, sync_seed, ids=("p2",))
    ] == ["v2"]
    enabled = query(session, sync_seed, operation_statuses=("ENABLE",))
    assert {r.display["main_material_id"] for r in enabled} == {"v1", "v2"}
    assert query(session, sync_seed, operation_statuses=("DISABLE",)) == ()
    spend = query(session, sync_seed, min_spend=Decimal("15"))
    assert [r.display["main_material_id"] for r in spend] == ["v2"]


def test_duplicate_directory_row_without_ad_material_id_is_hidden(session, sync_seed):
    # 目录对同一广告同一视频另有一条不带广告内素材 ID 的记录，不应重复成为使用位置。
    publish(session, sync_seed, "acc-1", [material_row("p1", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="a1",
            platform_material_id="v1",
            ad_material_id=None,
            material_type="VIDEO",
            name="",
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    (row,) = query(session, sync_seed)
    assert [u.ad_material_id for u in row.material_uses] == ["m1"]


def test_default_material_rows_match_full_listing_for_videos_with_facts(
    session, sync_seed
):
    """默认口径只为有事实的帖子查视频对照；有数据的视频行与列出全部对象时一致。"""
    publish(session, sync_seed, "acc-1", [material_row("p1", "10", "视频一")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    # 同一视频在另一账户的帖子当天没有事实：不贡献金额，使用位置仍归该视频行。
    use(
        session,
        sync_seed,
        adv="acc-2",
        ad="b1",
        video="v1",
        ad_material="m3",
        post="p9",
    )
    # 另一个只在目录里、当天没有数据的视频：默认不列出。
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a3",
        video="v2",
        ad_material="m4",
        post="p8",
    )

    def rows(include_empty):
        return {
            r.row_key: (r.display, r.metric_buckets, r.material_uses)
            for r in query(session, sync_seed, include_empty=include_empty)
        }

    default, full = rows(False), rows(True)
    assert list(default) == ['["material", "v1"]']
    assert default['["material", "v1"]'] == full['["material", "v1"]']
    display, buckets, uses = default['["material", "v1"]']
    assert buckets[0].values["spend"] == Decimal(10)
    assert {(u.ad_ref.advertiser_id, u.ad_ref.remote_id) for u in uses} == {
        ("acc-1", "a1"),
        ("acc-2", "b1"),
    }


def test_material_grouping_keeps_exact_case_and_unicode_post_identities(
    session, sync_seed
):
    """身份按原始字节精确区分；名称的大小写/Unicode展示不能合并平台ID。"""
    posts = ["same", "SAME", "é", "e\u0301", "素材"]
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row(post, str(index + 1)) for index, post in enumerate(posts)],
    )
    rows = query(session, sync_seed, include_empty=False)
    actual = {
        json.loads(row.row_key)[2]: row.metric_buckets[0].values["spend"]
        for row in rows
    }
    assert actual == {post: Decimal(index + 1) for index, post in enumerate(posts)}
