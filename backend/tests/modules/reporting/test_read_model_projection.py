"""派生日桶重建的金额、空值、类型纠正与版本围栏。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlmodel import select

from tests.modules.reporting.test_material_query import D, material_row, publish, use


def test_orphan_link_cleanup_does_not_recreate_account_projection(session, sync_seed):
    from app.modules.reporting.read_models.models import ReadBucketState
    from app.modules.reporting.store_models import MaterialPostLink

    link = MaterialPostLink(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="already-deleted",
        main_material_id="old-post",
        ad_remote_id="old-ad",
        main_material_type="VIDEO_SPARK_ADS",
        observed_at=datetime.now(UTC),
    )
    session.add(link)
    session.flush()
    session.delete(link)
    session.flush()
    assert not session.exec(
        select(ReadBucketState).where(
            ReadBucketState.tenant_id == sync_seed.tenant_id,
            ReadBucketState.advertiser_id == "already-deleted",
        )
    ).all()


def test_derived_bucket_cannot_prevent_deleting_account_after_canonical_cleanup(
    session, sync_seed
):
    from sqlalchemy import delete

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.ads.models import AdObject
    from app.modules.reporting.read_models.models import ReadBucketState

    account = AdvertiserAccount(
        tenant_id=sync_seed.tenant_id, advertiser_id="derived-cleanup", timezone="UTC"
    )
    session.add(account)
    session.flush()
    entity = AdObject(
        tenant_id=account.tenant_id,
        advertiser_id=account.advertiser_id,
        kind="ad",
        remote_id="removed",
        ad_type="REGULAR",
        observed_at=datetime.now(UTC),
        published_version=1,
    )
    session.add(entity)
    session.flush()
    # 清理程序可能先清派生元数据，之后才删除canonical行；源DELETE触发器
    # 会再次创建版本桶，但这个派生桶不能让最后一步账户删除失败。
    session.execute(
        delete(ReadBucketState).where(
            ReadBucketState.tenant_id == account.tenant_id,
            ReadBucketState.advertiser_id == account.advertiser_id,
        )
    )
    session.delete(entity)
    session.flush()
    session.delete(account)
    session.flush()
    assert not session.exec(
        select(ReadBucketState).where(
            ReadBucketState.tenant_id == sync_seed.tenant_id,
            ReadBucketState.advertiser_id == "derived-cleanup",
        )
    ).all()


def test_material_projection_rebuild_replaces_money_and_preserves_partial(
    session, sync_seed
):
    from app.modules.reporting.read_models.dirty_buckets import (
        ReadBucketKey,
        mark_dirty,
    )
    from app.modules.reporting.read_models.models import MaterialEntityDay
    from app.modules.reporting.read_models.projector import project_bucket

    rows = [material_row("p1", "10"), material_row("p2", "-")]
    publish(session, sync_seed, "acc-1", rows)
    for index in [1, 2]:
        use(
            session,
            sync_seed,
            adv="acc-1",
            ad=f"a{index}",
            video="v",
            ad_material=f"m{index}",
            post=f"p{index}",
        )
    key = ReadBucketKey(sync_seed.tenant_id, "acc-1", "material_day", D)
    revision = mark_dirty(session, [key])[key]
    assert project_bucket(session, key, expected_version=revision).published
    result = session.exec(
        select(MaterialEntityDay).where(
            MaterialEntityDay.tenant_id == sync_seed.tenant_id
        )
    ).one()
    assert result.spend == Decimal("10")
    assert result.spend_has_null is True
    assert project_bucket(session, key, expected_version=revision).published
    assert session.exec(
        select(MaterialEntityDay).where(
            MaterialEntityDay.tenant_id == sync_seed.tenant_id
        )
    ).one().spend == Decimal("10")

    publish(session, sync_seed, "acc-1", [material_row("p1", "25")])
    newer = mark_dirty(session, [key])[key]
    assert not project_bucket(session, key, expected_version=revision).published
    assert project_bucket(session, key, expected_version=newer).published
    session.expire_all()
    result = session.exec(
        select(MaterialEntityDay).where(
            MaterialEntityDay.tenant_id == sync_seed.tenant_id
        )
    ).one()
    assert result.spend == Decimal("25")
    assert result.spend_has_null is False


def test_material_type_correction_cannot_leave_old_video_amount(session, sync_seed):
    from app.modules.reporting.read_models.dirty_buckets import (
        ReadBucketKey,
        mark_dirty,
    )
    from app.modules.reporting.read_models.models import MaterialEntityDay
    from app.modules.reporting.read_models.projector import project_bucket

    publish(session, sync_seed, "acc-1", [material_row("post", "9")])
    use(
        session, sync_seed, adv="acc-1", ad="a", video="v", ad_material="m", post="post"
    )
    key = ReadBucketKey(sync_seed.tenant_id, "acc-1", "material_day", D)
    project_bucket(session, key, expected_version=mark_dirty(session, [key])[key])
    corrected = material_row("post", "11")
    corrected["dimensions"]["main_material_type"] = "UNKNOWN_TYPE"
    publish(session, sync_seed, "acc-1", [corrected])
    project_bucket(session, key, expected_version=mark_dirty(session, [key])[key])
    session.expire_all()
    result = session.exec(
        select(MaterialEntityDay).where(
            MaterialEntityDay.tenant_id == sync_seed.tenant_id
        )
    ).one()
    assert result.row_key == '["post", "acc-1", "post"]'
    assert result.spend == Decimal("11")


def test_unbuilt_material_is_unknown_then_dirty_bucket_stays_old_and_complete(
    session, sync_seed
):
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.query_helpers import finish_projections

    publish(session, sync_seed, "acc-1", [material_row("post", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad",
        video="video",
        ad_material="m",
        post="post",
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=D,
        include_empty=True,
        advertiser_ids=("acc-1",),
    )

    def query(**kw):
        return query_ads(
            session, context=sync_seed.context, bc_id="bc-a", filters=filters, **kw
        )

    missing = query()
    assert missing.coverage["status"] == "BUILDING"
    assert missing.metrics_coverage["unavailable_bucket_count"] == 1
    assert missing.summary["incomplete_dates"] == [str(D)]
    assert missing.items[0].metric_buckets[0].values["spend"] is None
    finish_projections(session)
    original = query()
    assert original.items[0].metric_buckets[0].values["spend"] == Decimal("10")
    publish(session, sync_seed, "acc-1", [material_row("post", "25")])
    stale = query(snapshot_id=original.snapshot.snapshot_id)
    assert stale.items[0].metric_buckets[0].values["spend"] == Decimal("10")
    assert stale.metrics_coverage["status"] == "STALE"
    assert stale.metrics_observed_at == original.metrics_observed_at
    finish_projections(session)
    latest = query(snapshot_id=original.snapshot.snapshot_id)
    assert latest.items[0].metric_buckets[0].values["spend"] == Decimal("25")
    assert latest.summary == original.summary
    assert latest.snapshot.snapshot_at == original.snapshot.snapshot_at
    assert latest.ordering_basis == "snapshot"


def test_current_usage_keeps_ties_but_frozen_row_does_not_guess_actionable_id(
    session, sync_seed
):
    from app.modules.ads.models import AdMaterialReference
    from app.modules.reporting.read_models.models import CurrentAdMaterial
    from tests.modules.reporting.test_material_query import query

    publish(session, sync_seed, "acc-1", [material_row("post", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad",
        video="video",
        ad_material="old",
        post="post",
    )
    for inner in ["new-a", "new-b"]:
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="ad",
                platform_material_id="video",
                ad_material_id=inner,
                material_type="VIDEO",
                complete=True,
                published_version=2,
            )
        )
    session.flush()
    projected = session.exec(
        select(CurrentAdMaterial).where(
            CurrentAdMaterial.tenant_id == sync_seed.tenant_id
        )
    ).all()
    assert {row.ad_material_id for row in projected} == {"new-a", "new-b"}
    row = query(session, sync_seed)[0]
    assert row.metric_buckets[0].values["spend"] == Decimal("10")
    assert len(row.material_uses) == 1
    assert row.material_uses[0].ad_material_id is None


def test_material_fallback_name_keeps_latest_per_post_before_video_minimum(
    session, sync_seed
):
    from datetime import timedelta
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_material_day
    from app.modules.reporting.schemas import ReportingFilter
    from app.modules.reporting.store_models import MaterialPostLink
    from tests.modules.reporting.query_helpers import query_ads

    for post in ["p1", "p2"]:
        session.add(
            MaterialPostLink(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id=post,
                main_material_type="VIDEO_SPARK_ADS",
                ad_remote_id=f"ad-{post}",
                platform_material_id="video",
                observed_at=datetime.now(UTC),
            )
        )
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10", "a-old"), material_row("p2", "20", "b-retained")],
    )
    tomorrow = D + timedelta(days=1)
    newer = material_row("p1", "5", "z-new")
    newer["dimensions"]["stat_time_day"] = str(tomorrow)
    publish_material_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=tomorrow,
        end=tomorrow,
        rows=[newer],
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=tomorrow,
        advertiser_ids=("acc-1",),
        query="b-retained",
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert len(page.items) == 1
    assert page.items[0].display["name"] == "b-retained"
    assert page.items[0].metric_buckets[0].values["spend"] == Decimal("35")
