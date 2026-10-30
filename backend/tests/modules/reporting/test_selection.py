from datetime import UTC, date, datetime

from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.reporting.schemas import ReportingFilter, SelectionRequest


def test_material_selection_keeps_usage_ids(report_case):
    from app.modules.reporting.selection import freeze_selection
    from tests.modules.reporting.query_helpers import query_ads

    # The material view consumes the typed ad usage identity; platform VID alone
    # cannot represent two uses in the same ad account.
    campaign = report_case.seed_campaign("Provider-Drama", "report-account", 1, 2)
    campaign_object = report_case.session.get(
        AdObject,
        (campaign.tenant_id, campaign.advertiser_id, campaign.kind, campaign.remote_id),
    )
    assert campaign_object is not None
    ad = AdObject(
        tenant_id=campaign.tenant_id,
        advertiser_id=campaign.advertiser_id,
        kind="ad",
        remote_id="ad-1",
        parent_kind="campaign",
        parent_remote_id=campaign.remote_id,
        ad_type="REGULAR",
        name="ad-1",
        observed_at=datetime.now(UTC),
        published_version=1,
        source_connection_id=campaign_object.source_connection_id,
        source_channel="OFFICIAL_API",
    )
    report_case.session.add(ad)
    usage_a = AdMaterialReference(
        tenant_id=report_case.context.tenant_id,
        advertiser_id="report-account",
        ad_remote_id="ad-1",
        platform_material_id="vid-1",
        ad_material_id="creative-1",
        material_type="VIDEO",
        name="one",
        main_material_id="vid-1",
        main_material_type="VIDEO",
        complete=True,
        published_version=1,
    )
    usage_b = AdMaterialReference(
        tenant_id=report_case.context.tenant_id,
        advertiser_id="report-account",
        ad_remote_id="ad-1",
        platform_material_id="vid-2",
        ad_material_id="creative-2",
        material_type="VIDEO",
        name="two",
        main_material_id="vid-2",
        main_material_type="VIDEO",
        complete=True,
        published_version=1,
    )
    report_case.session.add_all([usage_a, usage_b])
    report_case.session.flush()
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_material_day
    from app.modules.reporting.store_models import MaterialPostLink
    from tests.modules.reporting.test_material_query import material_row

    day = date(2026, 9, 30)
    # 素材日报主素材是帖子；对照把帖子归到广告内素材，素材行按视频冻结使用位置。
    rows = [material_row(post, "1") for post in ("post-1", "post-2")]
    for row in rows:
        row["dimensions"]["stat_time_day"] = str(day)
    for post, creative in (("post-1", "creative-1"), ("post-2", "creative-2")):
        report_case.session.add(
            MaterialPostLink(
                tenant_id=report_case.context.tenant_id,
                advertiser_id="report-account",
                main_material_id=post,
                ad_remote_id="ad-1",
                main_material_type="VIDEO_SPARK_ADS",
                ad_material_id=creative,
                platform_material_id={"creative-1": "vid-1", "creative-2": "vid-2"}[
                    creative
                ],
                observed_at=datetime.now(UTC),
            )
        )
    publish_material_day(
        report_case.session,
        tenant_id=report_case.context.tenant_id,
        job_id=uuid4(),
        accounts={"report-account": AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=rows,
    )
    filters = ReportingFilter(
        dimension="material", start_date=date(2026, 9, 30), end_date=date(2026, 9, 30)
    )
    page = query_ads(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=filters,
    )
    selection = freeze_selection(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        request=SelectionRequest(
            snapshot_id=page.snapshot.snapshot_id, mode="ALL_MATCHING"
        ),
    )
    assert {use.ad_material_id for use in selection.material_uses} >= {
        "creative-1",
        "creative-2",
    }
