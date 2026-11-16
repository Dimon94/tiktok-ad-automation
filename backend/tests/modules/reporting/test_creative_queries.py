"""真实 PostgreSQL 查询回归：创意经济事实先筛选再汇总。"""

import json
from decimal import Decimal

from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.test_creative_projection import build, identity
from tests.modules.reporting.test_creative_reporting import D, creative_row


def query(session, seed, **kwargs):
    return build_rows(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D, **kwargs
        ),
    )


def test_shared_video_filter_precedes_aggregation(session, sync_seed):
    identity(session, sync_seed, "first")
    second = identity(session, sync_seed, "second")
    second.ad_remote_id = "ad-2"
    second.ad_material_id = "am-2"
    session.add(second)
    build(
        session,
        sync_seed,
        ad_spend="50",
        rows=[creative_row("first", "20", "10"), creative_row("second", "30", "20")],
    )
    rows = query(session, sync_seed)
    video = next(r for r in rows if json.loads(r.row_key)[0] == "material")
    assert video.metric_buckets[0].values["spend"] == Decimal("50")
    filtered = query(session, sync_seed, ids=("ad-1",))
    assert len(filtered) == 1
    assert filtered[0].metric_buckets[0].values["spend"] == Decimal("20")
    assert filtered[0].coverage["unallocated_note"]
    assert not filtered[0].material_uses  # 历史 identity 不能变成当前目录动作。


def test_unknown_signed_residual_and_composite_preserved(session, sync_seed):
    identity(
        session,
        sync_seed,
        "carousel",
        assets=[
            {"material_type": "IMAGE", "platform_material_id": "i1"},
            {"material_type": "IMAGE", "platform_material_id": "i2"},
        ],
    )
    build(
        session,
        sync_seed,
        ad_spend="100",
        rows=[creative_row("carousel", "90", "20"), creative_row("unknown", "11", "-")],
    )
    rows = query(session, sync_seed)
    by_kind = {json.loads(r.row_key)[0]: r for r in rows}
    assert set(by_kind) == {"carousel", "unmapped_creative", "unattributed"}
    assert by_kind["carousel"].metric_buckets[0].values["spend"] == Decimal("90")
    assert len(by_kind["carousel"].coverage["assets"][0]["assets"]) == 2
    assert by_kind["unmapped_creative"].metric_buckets[0].values["spend"] == Decimal(
        "11"
    )
    assert by_kind["unattributed"].metric_buckets[0].values["spend"] == Decimal("-1")
    assert by_kind["unattributed"].metric_buckets[0].values["revenue_total"] is None
    assert all(not r.material_uses for r in rows)


def test_material_namespace_preserves_type(session, sync_seed):
    identity(session, sync_seed, "video")
    image = identity(session, sync_seed, "image")
    image.material_type = "IMAGE"
    session.add(image)
    build(
        session,
        sync_seed,
        ad_spend="50",
        rows=[creative_row("video", "20", "10"), creative_row("image", "30", "20")],
    )
    rows = query(session, sync_seed)
    keys = {tuple(json.loads(r.row_key)) for r in rows}
    assert ("material", "VIDEO", "vid-1") in keys
    assert ("material", "IMAGE", "vid-1") in keys


def test_snapshot_summary_page_export_and_actions_obey_ad_filter(session, sync_seed):
    from uuid import uuid4

    from app.modules.reporting.exports import _csv_rows, _export_rows
    from app.modules.reporting.query_models import ReportExport
    from app.modules.reporting.schemas import SelectionRequest
    from app.modules.reporting.selection import freeze_selection
    from tests.modules.reporting.query_helpers import query_ads
    from tests.modules.reporting.test_material_query import material_row, publish, use

    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("c1", "20"), material_row("c2", "30"), material_row("c3", "10")],
    )
    for c, ad, vid in [
        ("c1", "ad-1", "shared"),
        ("c2", "ad-2", "shared"),
        ("c3", "ad-1", "small"),
    ]:
        use(session, sync_seed, adv="acc-1", ad=ad, video=vid, ad_material=c, post=c)
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=D,
        ids=("ad-1",),
        sort_by="spend",
        sort_direction="desc",
    )
    first = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters, limit=1
    )
    second = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        snapshot_id=first.snapshot.snapshot_id,
        cursor=first.next_cursor,
        limit=1,
    )
    assert first.total == 2 and first.items[0].display["main_material_id"] == "shared"
    assert first.items[0].metric_buckets[0].values["spend"] == 20
    assert second.items[0].metric_buckets[0].values["spend"] == 10
    assert Decimal(first.summary["buckets"][0]["values"]["spend"]) == 30
    selection = freeze_selection(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        request=SelectionRequest(
            snapshot_id=first.snapshot.snapshot_id, mode="ALL_MATCHING"
        ),
    )
    assert {u.ad_ref.remote_id for u in selection.material_uses} == {"ad-1"}
    assert {u.ad_material_id for u in selection.material_uses} == {"c1", "c3"}
    from app.modules.reporting.exports import create_export

    export_id = create_export(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=first.snapshot.snapshot_id,
        idempotency_key=str(uuid4()),
    ).id
    export = session.get(ReportExport, export_id)
    rows = list(_export_rows(session, export))
    # 五类冻结导出中只有普通实例参与素材合计，父控制/对账各自保留原行。
    instances = [row for row in rows if row["row_kind"] == "INSTANCE"]
    assert [Decimal(r["metric_buckets"][0]["values"]["spend"]) for r in instances] == [
        20,
        10,
    ]
    assert "第 29 天" in _csv_rows(export, rows).decode()
    from urllib.parse import quote

    from app.modules.reporting.trend import build_trend

    trend = build_trend(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        level="material",
        advertiser_ids=("acc-1",),
        ids=("material:" + quote(first.items[0].row_key),),
        start=D,
        end=D,
        granularity="day",
        snapshot_id=first.snapshot.snapshot_id,
    )
    assert trend.points[0].buckets[0].values["spend"] == 20


def test_material_trend_uses_namespaced_creative_money(session, sync_seed):
    from app.modules.reporting.trend import build_trend

    identity(session, sync_seed, "video")
    image = identity(session, sync_seed, "image")
    image.material_type = "IMAGE"
    session.add(image)
    build(
        session,
        sync_seed,
        ad_spend="50",
        rows=[creative_row("video", "20", "10"), creative_row("image", "30", "20")],
    )
    result = build_trend(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        level="material",
        advertiser_ids=("acc-1",),
        ids=(json.dumps(["material", "IMAGE", "vid-1"]),),
        start=D,
        end=D,
        granularity="day",
    )
    assert result.points[0].buckets[0].values["spend"] == 30
    assert result.points[0].buckets[0].availability["revenue_d3"] != "UNSUPPORTED"


def test_ad_detail_only_counts_exact_current_creative_position(session, sync_seed):
    from app.modules.reporting.ad_materials import (
        freeze_ad_material_selection,
        list_ad_materials,
    )
    from tests.modules.reporting.query_helpers import finish_projections
    from tests.modules.reporting.test_material_query import material_row, publish, use

    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("c1", "20"), material_row("ad-1", "30")],
    )
    for c, ad in [("c1", "ad-1"), ("ad-1", "ad-2")]:
        use(
            session,
            sync_seed,
            adv="acc-1",
            ad=ad,
            video="shared",
            ad_material=c,
            post=c,
        )
    finish_projections(session)
    result = list_ad_materials(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="ad-1",
        start=D,
        end=D,
    )
    assert result.metric_scope == "CREATIVE"
    assert (
        len(result.items) == 1
        and result.items[0].metric_buckets[0].values["spend"] == 20
    )
    assert result.items[0].creative_ids == ("c1",)
    assert result.creative_rows[0].metric_buckets[0].values["spend"] == 20
    selected = freeze_ad_material_selection(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="ad-1",
        ad_material_ids=("c1",),
    )
    assert {u.ad_ref.remote_id for u in selected.material_uses} == {"ad-1"}


def test_projection_revision_and_account_boundaries(session, sync_seed):
    from app.modules.reporting.read_models.models import ReadBucketState
    from tests.modules.reporting.query_helpers import finish_projections
    from tests.modules.reporting.test_material_query import material_row, publish, use

    for adv, spend in [("acc-1", "20"), ("acc-2", "900")]:
        publish(session, sync_seed, adv, [material_row("same", spend)])
        use(
            session,
            sync_seed,
            adv=adv,
            ad="same-ad",
            video="same-video",
            ad_material="am",
            post="same",
        )
    finish_projections(session)
    rows = query(session, sync_seed, advertiser_ids=("acc-1",))
    assert len(rows) == 1 and rows[0].metric_buckets[0].values["spend"] == 20
    state = session.get(
        ReadBucketState, (sync_seed.tenant_id, "acc-1", "creative_material", D)
    )
    state.source_revision += 1
    session.add(state)
    session.flush()
    rows = query(session, sync_seed, advertiser_ids=("acc-1",))
    assert rows[0].coverage["projection"]["status"] == "STALE"
    assert rows[0].metric_buckets[0].availability["spend"] == "PARTIAL"


def test_old_material_cache_and_snapshot_contracts_are_rejected(session, sync_seed):
    import pytest
    from fastapi import HTTPException

    from app.modules.reporting.query_models import QuerySnapshot, read_snapshot
    from app.modules.reporting.query_reuse import _digest, query_key
    from app.modules.reporting.schemas import MATERIAL_QUERY_CONTRACT
    from tests.modules.reporting.query_helpers import query_ads
    from tests.modules.reporting.test_material_query import material_row, publish

    filters = ReportingFilter(dimension="material", start_date=D, end_date=D)
    old_key = _digest(
        [
            sync_seed.tenant_id,
            "bc-a",
            sync_seed.actor_id,
            filters.model_dump(mode="json"),
        ]
    )
    assert query_key(sync_seed.context, "bc-a", filters) != old_key
    publish(session, sync_seed, "acc-1", [material_row("unknown", "11")])
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    snapshot = session.get(QuerySnapshot, page.snapshot.snapshot_id)
    assert (
        snapshot.publication_versions["material_query_contract"]
        == MATERIAL_QUERY_CONTRACT
    )
    # 缺失合同及容量迁移前的 v3 都不能复用为当前快照。
    for old_versions in (
        {"reporting": 1},
        snapshot.publication_versions | {"material_query_contract": 3},
    ):
        snapshot.publication_versions = old_versions
        session.add(snapshot)
        session.flush()
        with pytest.raises(HTTPException) as exc:
            read_snapshot(
                session,
                context=sync_seed.context,
                bc_id="bc-a",
                snapshot_id=snapshot.id,
            )
        assert (
            exc.value.status_code == 409
            and exc.value.detail == "query_snapshot_contract_changed"
        )


def test_same_video_keeps_currency_buckets_separate(session, sync_seed):
    from sqlalchemy import text

    from tests.modules.reporting.query_helpers import finish_projections
    from tests.modules.reporting.test_material_query import material_row, publish, use

    for adv, spend in [("acc-1", "20"), ("acc-2", "30")]:
        publish(session, sync_seed, adv, [material_row("same", spend)])
        use(
            session,
            sync_seed,
            adv=adv,
            ad="ad",
            video="same-video",
            ad_material="am",
            post="same",
        )
    for table in ("creative_report_day", "ad_report_day"):
        session.execute(
            text(
                f"UPDATE {table} SET currency='EUR' WHERE tenant_id=:t AND advertiser_id='acc-2'"
            ),
            {"t": sync_seed.tenant_id},
        )
    finish_projections(session)
    rows = query(session, sync_seed)
    assert len(rows) == 1
    assert {b.currency: b.values["spend"] for b in rows[0].metric_buckets} == {
        "USD": Decimal("20"),
        "EUR": Decimal("30"),
    }


def test_scoped_keys_reject_other_account_and_legacy_post_namespace():
    import pytest
    from fastapi import HTTPException

    from app.modules.reporting.snapshot_members import _material_key

    for key in (
        ["post", "acc-1", "p"],
        ["material", "v"],
        ["unmapped_creative", "acc-2", "c"],
        ["unattributed", "acc-2", "g"],
    ):
        with pytest.raises(HTTPException):
            _material_key(json.dumps(key), {"acc-1"})
