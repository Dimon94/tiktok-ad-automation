"""轮播说明关联不能展开广告金额；使用真实 PostgreSQL。"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.integrations.tiktok.contracts.carousel import (
    CarouselCreativeIdentity,
    decode_carousels,
)
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.reporting.ad_materials import list_ad_materials
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.builds.test_carousel_execution import carousel_case as carousel_case
from tests.modules.reporting.query_helpers import build_rows
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_material_query import D
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


def creative(images, song, native=None):
    return {
        **({"smart_plus_creative_id": native} if native else {}),
        "creative_info": {
            "ad_format": "CAROUSEL_ADS",
            "image_info": [{"image_id": image} for image in images],
            "music_info": {"music_id": song},
        },
    }


def test_identity_keeps_order_song_and_optional_native_id():
    def identity(images, song):
        return CarouselCreativeIdentity("acc", "ad", None, images, song)

    first = identity(("a", "b"), "song")
    assert first != identity(("b", "a"), "song")
    assert first != identity(("a", "b"), "another-song")
    assert first.remote_creative_id is None
    with pytest.raises(ValueError):
        identity((), "song")


@pytest.mark.parametrize("native", [None, "native"])
@pytest.mark.parametrize("partial", [False, True])
def test_shared_images_do_not_expand_ad_metrics(session, sync_seed, native, partial):
    config = {
        "creative_list": [
            creative(("shared", "b"), "song-1", native),
            creative(("shared", "b"), "song-2"),
        ]
    }
    if partial:
        del config["creative_list"][0]["creative_info"]["music_info"]
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="ad",
            remote_id="a1",
            parent_kind="adgroup",
            parent_remote_id="g1",
            ad_type="SMART_PLUS",
            name="轮播",
            configuration=config,
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    for image in ("shared", "b"):
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="a1",
                platform_material_id=image,
                material_type="IMAGE",
                complete=True,
                published_version=1,
            )
        )
    session.flush()
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=D,
        end=D,
        rows=[ad_row("a1", D, spend="10", total="20")],
    )
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="ad", advertiser_ids=("acc-1",), start_date=D, end_date=D
        ),
    )
    assert len(rows) == 1
    assert rows[0].metric_buckets[0].values["spend"] == Decimal(10)
    assert rows[0].metric_buckets[0].values["revenue_total"] == Decimal(20)
    assert rows[0].display["creative_format"] == "CAROUSEL_ADS"
    assert len(rows[0].carousel_creatives) == (1 if partial else 2)
    assert {x.music_id for x in rows[0].carousel_creatives} == (
        {"song-2"} if partial else {"song-1", "song-2"}
    )
    result = list_ad_materials(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="a1",
        start=D,
        end=D,
    )
    assert result.items == ()
    assert len(result.carousel_creatives) == (1 if partial else 2)
    assert result.carousel_linkage_complete is not partial
    assert result.metric_scope == "AD"
    assert all(
        "metric_buckets" not in x for x in result.model_dump()["carousel_creatives"]
    )


def test_partial_content_never_fabricates_exact_identity():
    entry = creative(("a", "b"), "song")
    del entry["creative_info"]["music_info"]
    decoded = decode_carousels(
        {"creative_list": [entry]}, advertiser_id="acc", ad_id="ad", smart=True
    )
    assert decoded.is_carousel and not decoded.complete and decoded.identities == ()
    entry["creative_info"]["image_info"] = [{"file_name": "same.jpg"}]
    assert not decode_carousels(
        {"creative_list": [entry]}, advertiser_id="acc", ad_id="ad", smart=True
    ).complete


def test_images_never_enter_video_material_rules(session, sync_seed):
    from tests.modules.automation_rules.test_material_inputs import result, setup, use

    setup(session, sync_seed)
    image = use(session, sync_seed, "a1", "same-id")
    image.material_type = "IMAGE"
    session.add(image)
    session.flush()
    loaded, _ = result(session, sync_seed)
    assert not [e for e in loaded.entities.values() if e.kind == "material"]


def test_image_id_never_resolves_non_spark_video_identity(session, sync_seed):
    from sqlmodel import select

    from app.modules.reporting.material_links import _resolve_videos
    from app.modules.reporting.store_models import MaterialPostLink
    from tests.modules.reporting.test_material_query import material_row, publish, use

    row = material_row("post", "10")
    row["dimensions"]["main_material_type"] = "VIDEO_NON_SPARK_ADS"
    publish(session, sync_seed, "acc-1", [row])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="image",
        ad_material="m",
        post="post",
    )
    image = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == sync_seed.tenant_id
        )
    ).one()
    image.material_type = "IMAGE"
    link = session.exec(
        select(MaterialPostLink).where(
            MaterialPostLink.tenant_id == sync_seed.tenant_id
        )
    ).one()
    link.platform_material_id = None
    link.main_material_type = "VIDEO_NON_SPARK_ADS"
    session.add_all([image, link])
    session.flush()
    _resolve_videos(session, sync_seed.tenant_id, "acc-1")
    session.refresh(link)
    assert link.platform_material_id is None


@pytest.mark.parametrize("conclusion", ["CREATED", "RECONCILED"])
def test_frozen_jump_requires_exact_successful_account_ad_evidence(
    carousel_case, conclusion
):
    from sqlmodel import Session

    from app.modules.builds.execution_models import ExecutionStep, StepEvidence
    from app.modules.reporting.carousel_links import frozen_carousel_links
    from tests.modules.builds.test_carousel_execution import claim_ad

    engine, context, route, submission_id, _, _ = carousel_case
    claim = claim_ad(carousel_case)
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, claim.step_id)
        kwargs = {
            "tenant_id": context.tenant_id,
            "bc_id": route.bc_id,
            "advertiser_id": "account-A",
            "ad_id": "actual-ad",
        }
        step.remote_id, step.status = "actual-ad", "SUCCEEDED"
        session.add(step)
        session.flush()
        assert frozen_carousel_links(session, **kwargs) == ()
        session.add(
            StepEvidence(
                tenant_id=context.tenant_id,
                submission_id=submission_id,
                step_id=step.id,
                attempt=step.attempt,
                conclusion="LATE_CREATED",
                summary={"remote_id": "actual-ad"},
            )
        )
        session.flush()
        assert frozen_carousel_links(session, **kwargs) == ()
        session.add(
            StepEvidence(
                tenant_id=context.tenant_id,
                submission_id=submission_id,
                step_id=step.id,
                attempt=step.attempt,
                conclusion=conclusion,
                summary={"remote_id": "actual-ad", "mismatch": False},
            )
        )
        session.flush()
        links = frozen_carousel_links(session, **kwargs)
        assert len(links) == 1 and links[0].planned_ad_id == step.planned_ad_id
        for patch in (
            {"tenant_id": uuid4()},
            {"bc_id": "other"},
            {"advertiser_id": "account-B"},
            {"ad_id": "other"},
        ):
            assert frozen_carousel_links(session, **(kwargs | patch)) == ()
        readback = ExecutionStep(
            tenant_id=step.tenant_id,
            submission_id=step.submission_id,
            preview_id=step.preview_id,
            bc_id=step.bc_id,
            unit_id=step.unit_id,
            group_id=step.group_id,
            planned_ad_id=step.planned_ad_id,
            parent_step_id=step.id,
            kind="READBACK",
            step_key="report-test-readback",
        )
        readback.mismatch = True
        session.add(readback)
        session.flush()
        assert frozen_carousel_links(session, **kwargs) == ()
        readback.mismatch = False
        session.add(readback)
        session.flush()
        step.mismatch = True
        session.add(step)
        session.flush()
        assert frozen_carousel_links(session, **kwargs) == ()
        step.mismatch, step.status = False, "UNKNOWN"
        session.add(step)
        session.flush()
        assert frozen_carousel_links(session, **kwargs) == ()


def test_image_report_rows_do_not_block_existing_video_rule(session, sync_seed):
    from app.modules.reporting.store_models import MaterialReportDay
    from tests.modules.automation_rules.test_inputs import NOW, TODAY
    from tests.modules.automation_rules.test_material_inputs import (
        post,
        result,
        setup,
        use,
    )

    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "video-post")
    session.add(
        MaterialReportDay(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            stat_date=TODAY,
            main_material_id="image-post",
            main_material_type="IMAGE",
            currency="USD",
            timezone="Asia/Hong_Kong",
            fetched_at=NOW,
            sync_job_id=uuid4(),
            spend=Decimal(100),
        )
    )
    session.flush()
    loaded, _ = result(session, sync_seed)
    videos = [e for e in loaded.entities.values() if e.kind == "material"]
    assert len(videos) == 1 and videos[0].judgement_issue is None


@pytest.mark.parametrize(
    "patch",
    [
        {"image_info": "bad"},
        {"music_info": "bad"},
        {"image_info": [{"image_id": "a", "web_uri": "b"}]},
        {"video_info": {"video_id": "v"}},
    ],
)
def test_carousel_rejects_malformed_or_conflicting_content(patch):
    entry = creative(("a",), "song")
    entry["creative_info"].update(patch)
    with pytest.raises(ValueError):
        decode_carousels(
            {"creative_list": [entry]}, advertiser_id="acc", ad_id="ad", smart=True
        )


def test_explicit_carousel_with_missing_list_stays_incomplete():
    decoded = decode_carousels(
        {"ad_format": "CAROUSEL_ADS"}, advertiser_id="acc", ad_id="ad", smart=True
    )
    assert decoded.is_carousel and not decoded.complete and not decoded.identities


def test_video_cover_and_post_keep_original_namespaces():
    for info in (
        {"video_info": {"video_id": "v"}, "image_info": [{"image_id": "cover"}]},
        {"tiktok_item_id": "post", "image_info": [{"image_id": "cover"}]},
    ):
        decoded = decode_carousels(
            {"creative_list": [{"creative_info": info}]},
            advertiser_id="acc",
            ad_id="ad",
            smart=True,
        )
        assert not decoded.is_carousel and not decoded.identities


def test_flat_image_directory_stays_incomplete_without_unit_money(session, sync_seed):
    from sqlmodel import select

    from tests.modules.reporting.test_material_query import use

    use(session, sync_seed, adv="acc-1", ad="ad", video="image", ad_material="m")
    row = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == sync_seed.tenant_id
        )
    ).one()
    row.material_type = "IMAGE"
    session.add(row)
    session.flush()
    result = list_ad_materials(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="ad",
        start=D,
        end=D,
    )
    assert result.metric_scope == "AD" and not result.carousel_linkage_complete
    assert result.items == () and result.carousel_creatives == ()


@pytest.fixture
def mixed_mid_case(session, sync_seed):
    from datetime import timedelta

    from app.modules.reporting.store_models import MaterialReportDay
    from tests.modules.automation_rules.test_inputs import NOW, TODAY
    from tests.modules.automation_rules.test_material_inputs import post, setup, use

    def seed(other_type, video_spend="10"):
        setup(session, sync_seed)
        use(session, sync_seed, "a1")
        post(session, sync_seed, "a1", "same-mid", spend=video_spend, revenue="0")
        start = TODAY - timedelta(days=1)
        session.add(
            MaterialReportDay(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                stat_date=start,
                main_material_id="same-mid",
                main_material_type=other_type,
                currency="USD",
                timezone="Asia/Hong_Kong",
                fetched_at=NOW,
                sync_job_id=uuid4(),
                spend=Decimal(100),
                revenue_total=Decimal(200),
            )
        )
        session.flush()
        return start, TODAY

    return seed


@pytest.mark.parametrize("other_type", ["IMAGE", "UNKNOWN_FUTURE"])
def test_mixed_mid_rules_exclude_nonvideo_facts(
    session, sync_seed, mixed_mid_case, other_type
):
    from tests.modules.automation_rules.test_material_inputs import result

    mixed_mid_case(other_type)
    loaded, _ = result(session, sync_seed)
    video = next(e for e in loaded.entities.values() if e.kind == "material")
    assert loaded.facts["LIFETIME"][video.key]["spend"] == Decimal(10)
    assert loaded.facts["LIFETIME"][video.key]["revenue_total"] == Decimal(0)


@pytest.mark.parametrize("other_type", ["IMAGE", "UNKNOWN_FUTURE"])
def test_mixed_mid_rule_value_window_ignores_nonvideo_money(
    session, sync_seed, mixed_mid_case, other_type
):
    from app.modules.automation_rules.inputs import AccountInfo, _facts
    from tests.modules.automation_rules.test_inputs import NOW

    mixed_mid_case(other_type, video_spend="0")
    assert (
        _facts(
            session,
            sync_seed.tenant_id,
            {"acc-1": AccountInfo("acc-1", "bc-a", "Asia/Hong_Kong", "USD", "A")},
            "LIFETIME",
            material=True,
            now=NOW,
        )
        == {}
    )


@pytest.mark.parametrize("other_type", ["IMAGE", "UNKNOWN_FUTURE"])
def test_mixed_mid_ad_materials_exclude_nonvideo_facts(
    session, sync_seed, mixed_mid_case, other_type
):
    start, end = mixed_mid_case(other_type)
    result = list_ad_materials(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="a1",
        start=start,
        end=end,
    )
    assert len(result.items) == 1
    assert result.items[0].metric_buckets[0].values["spend"] == Decimal(10)
    assert result.items[0].metric_buckets[0].values["revenue_total"] == Decimal(0)


@pytest.mark.parametrize("other_type", ["IMAGE", "UNKNOWN_FUTURE"])
@pytest.mark.parametrize("include_empty", [False, True])
def test_mixed_mid_material_report_splits_typed_money(
    session, sync_seed, mixed_mid_case, other_type, include_empty
):
    import json

    from app.modules.reporting.trend import build_trend

    start, end = mixed_mid_case(other_type)
    filters = ReportingFilter(
        dimension="material",
        advertiser_ids=("acc-1",),
        start_date=start,
        end_date=end,
        include_empty=include_empty,
        sort_by="spend",
        sort_direction="desc",
    )
    rows = build_rows(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert [json.loads(r.row_key)[0] for r in rows] == ["post", "material"]
    assert [r.metric_buckets[0].values["spend"] for r in rows] == [
        Decimal(100),
        Decimal(10),
    ]
    assert sum(r.metric_buckets[0].values["revenue_total"] for r in rows) == Decimal(
        200
    )
    filtered = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"min_spend": Decimal(50)}),
    )
    assert len(filtered) == 1 and json.loads(filtered[0].row_key)[0] == "post"
    for identity, expected in (("v1", Decimal(10)), ("same-mid", Decimal(100))):
        trend = build_trend(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            level="material",
            advertiser_ids=("acc-1",),
            ids=(identity,),
            start=start,
            end=end,
            granularity="day",
        )
        assert (
            sum(v.values["spend"] or 0 for p in trend.points for v in p.buckets)
            == expected
        )


@pytest.mark.parametrize(
    "info",
    [{}, {"image_info": []}, {"image_info": [], "music_info": {"music_id": "song"}}],
)
def test_top_level_carousel_empty_entry_stays_incomplete(info):
    result = decode_carousels(
        {"ad_format": "CAROUSEL_ADS", "creative_list": [{"creative_info": info}]},
        advertiser_id="acc",
        ad_id="ad",
        smart=True,
    )
    assert result.is_carousel and not result.complete and result.identities == ()


@pytest.mark.parametrize("carousel", [True, False])
def test_missing_creative_parent_never_invents_ad_identity(carousel):
    from app.modules.ads.directory import directory_carousels

    entity = AdObject(
        tenant_id=uuid4(),
        advertiser_id="synthetic-account",
        kind="creative",
        remote_id="synthetic-creative",
        parent_remote_id=None,
        ad_type="SMART_PLUS",
        name="synthetic",
        observed_at=datetime.now(UTC),
        published_version=1,
        configuration={
            "ad_format": "CAROUSEL_ADS",
            "image_ids": ["image"],
            "music_id": "song",
        }
        if carousel
        else {"video_id": "video"},
    )
    result = directory_carousels(entity)
    assert not result.identities
    assert result.is_carousel is carousel
    assert result.complete is (not carousel)
