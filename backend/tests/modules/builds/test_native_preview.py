"""Native prepare → frozen preview, real DB and SDK transport only."""

import copy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.ads.models import AdObject
from app.modules.builds import previews
from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from tests.modules.builds.scene_jobs import test_native_series as native
from tests.modules.builds.scene_jobs.test_native_series import (
    job_env as job_env,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    prepared_native_workspace,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    scene_env as scene_env,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    source_env as source_env,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    wire as wire,
)


def test_prepared_workspace_freezes_native_plan(job_env, wire, redis_client):
    draft_id, _, expected = prepared_native_workspace(job_env, wire, redis_client, True)
    with Session(native.engine, expire_on_commit=False) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        identity = previews.generate_preview(
            session,
            context=job_env["context"],
            draft_id=draft_id,
            expected_revision=draft.revision,
        )
        result = previews.get_preview_summary(
            session, context=job_env["context"], preview_id=identity
        )
        assert result.status == "FROZEN"
        assert (result.campaign_count, result.adgroup_count, result.ad_count) == (
            1,
            1,
            2,
        )
        frozen = result.native_request
        assert frozen["selection"]["native_series_id"] == "exact-series"
        assert [
            (x["material_id"], x["video_id"], x["image_id"])
            for x in frozen["materials"]
        ] == expected
        assert frozen["campaign"]["budget"] == "100.25"
        assert len(frozen["ads"]) == 2
        assert all(
            len(ad["materials"]) == 2 and ad["text"] and ad["cta"]["recommend_assets"]
            for ad in frozen["ads"]
        )
        assert result.content_digest
        units = previews.get_preview_units(
            session, context=job_env["context"], preview_id=identity
        )
        assert units.total == 1
        groups = previews.get_frozen_groups(
            session, context=job_env["context"], unit_id=units.items[0].unit_id
        )
        assert groups.total == 1
        ads = previews.get_frozen_ads(
            session,
            context=job_env["context"],
            unit_id=units.items[0].unit_id,
            group_id=groups.items[0].group_id,
        )
        assert ads.total == 2
        assert [ad.text for ad in ads.items] == [ad["text"] for ad in frozen["ads"]]
        assert (
            previews.generate_preview(
                session,
                context=job_env["context"],
                draft_id=draft_id,
                expected_revision=draft.revision,
            )
            == identity
        )


@pytest.mark.parametrize(
    "count,changes,total,widths",
    [
        (
            6,
            {
                "group_generation_mode": "FIXED",
                "group_count": 1,
                "group_material_allocation": "SHARED",
                "max_materials_per_group": None,
                "ads_per_group": 1,
                "creative_count": 6,
            },
            6,
            [6] * 6,
        ),
        (
            6,
            {
                "ads_per_group": 2,
                "creative_count": 3,
                "ad_material_allocation": "SEQUENTIAL_AVERAGE",
            },
            6,
            [3] * 6,
        ),
        (
            30,
            {
                "max_materials_per_group": 30,
                "ad_generation_mode": "BY_MATERIAL",
                "ads_per_group": None,
                "ad_material_allocation": None,
                "max_materials_per_ad": 1,
            },
            30,
            [1] * 30,
        ),
    ],
)
def test_native_structure_allocations_and_obsolete(
    job_env, wire, redis_client, count, changes, total, widths
):
    draft_id, _, _ = prepared_native_workspace(
        job_env, wire, redis_client, True, count=count, config_changes=changes
    )
    with Session(native.engine, expire_on_commit=False) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        pid = previews.generate_preview(
            session,
            context=job_env["context"],
            draft_id=draft_id,
            expected_revision=draft.revision,
        )
        result = previews.get_preview_summary(
            session, context=job_env["context"], preview_id=pid
        )
        frozen = copy.deepcopy(result.native_request)
        assert result.ad_count == total
        assert [len(ad["materials"]) for ad in frozen["ads"]] == widths
        assert result.daily_budget_sum == Decimal("100.25")
        assert frozen["campaign"]["budget"] == "100.25"
        for base in {ad["base_ad_no"] for ad in frozen["ads"]}:
            variants = [ad for ad in frozen["ads"] if ad["base_ad_no"] == base]
            assert len({ad["text"] for ad in variants}) == len(variants)
            assert all(
                ad["materials"] == variants[0]["materials"]
                and ad["cta"] == variants[0]["cta"]
                for ad in variants
            )
        unit = session.exec(select(BuildUnit).where(BuildUnit.preview_id == pid)).one()
        loaded = previews.load_frozen_unit(
            session, context=job_env["context"], unit_id=unit.id
        )
        assert loaded.native_request == frozen
        old_digest = result.content_digest
        # Existing DB revision trigger must obsolete, while frozen history remains byte-equivalent.
        draft.revision += 1
        session.add(draft)
        session.flush()
        result = previews.get_preview_summary(
            session, context=job_env["context"], preview_id=pid
        )
        assert result.status == "OBSOLETE"
        assert result.native_request == frozen and result.content_digest == old_digest


@pytest.mark.parametrize(
    "case",
    [
        "active",
        "inactive",
        "unknown",
        "other_series",
        "other_region",
        "missing_video",
        "too_many_groups",
        "no_cta",
    ],
)
def test_native_freeze_failures_and_occupancy(job_env, wire, redis_client, case):
    changes = {"max_materials_per_group": 1} if case == "too_many_groups" else {}
    draft_id, _, expected = prepared_native_workspace(
        job_env, wire, redis_client, True, config_changes=changes
    )
    with Session(native.engine, expire_on_commit=False) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        if case in {"active", "inactive", "unknown", "other_series", "other_region"}:
            session.add(
                AdObject(
                    tenant_id=job_env["context"].tenant_id,
                    advertiser_id="actual-account",
                    kind="adgroup",
                    remote_id="conflict-1",
                    ad_type="SMART_PLUS",
                    configuration={
                        "native_series_id": "other"
                        if case == "other_series"
                        else "exact-series",
                        "location_ids": [
                            "6251999" if case == "other_region" else "6252001"
                        ],
                    },
                    operation_status="DELETED"
                    if case == "inactive"
                    else "DISABLE"
                    if case == "unknown"
                    else "ENABLE",
                    delivery_status="ACTIVE" if case == "active" else None,
                    observed_at=datetime.now(UTC),
                    published_version=1,
                )
            )
        if case == "missing_video":
            from app.modules.materials.models import AccountMaterial

            mapping = session.exec(
                select(AccountMaterial).where(
                    AccountMaterial.material_id == expected[0][0]
                )
            ).one()
            mapping.status = "unavailable"
            session.add(mapping)
        if case == "no_cta":
            job = session.exec(
                select(native.SceneJob).where(
                    native.SceneJob.native_selection.is_not(None)
                )
            ).one()
            job.facts = {**job.facts, "cta": {}}
            session.add(job)
        session.flush()
        failure = {
            "active": "native_series_active_conflict",
            "missing_video": "native_preview_not_ready",
            "too_many_groups": "native_structure_unsupported",
            "no_cta": "cta_unavailable",
        }.get(case)
        if failure:
            with pytest.raises(DomainError) as caught:
                previews.generate_preview(
                    session,
                    context=job_env["context"],
                    draft_id=draft_id,
                    expected_revision=draft.revision,
                )
            assert caught.value.code == failure
            assert not session.exec(
                select(BuildPreview).where(BuildPreview.draft_id == draft_id)
            ).all()
        else:
            pid = previews.generate_preview(
                session,
                context=job_env["context"],
                draft_id=draft_id,
                expected_revision=draft.revision,
            )
            result = previews.get_preview_summary(
                session, context=job_env["context"], preview_id=pid
            )
            conflicts = result.native_request["occupancy"]["visible_conflicts"]
            assert len(conflicts) == (
                0 if case in {"other_series", "other_region"} else 1
            )
            if conflicts:
                assert conflicts[0]["state"] == (
                    "INACTIVE" if case == "inactive" else "UNKNOWN"
                )
                assert conflicts[0]["locations"] == ["6252001"]


def test_concurrent_native_preview_one_revision_and_stable_digest(
    job_env, wire, redis_client
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.modules.builds.frozen_requests import native_request_digest

    draft_id, _, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    with Session(native.engine) as session:
        revision = session.get(BuildDraft, draft_id).revision
    barrier = Barrier(2)

    def freeze():
        barrier.wait(timeout=10)
        with Session(native.engine) as session, session.begin():
            return previews.generate_preview(
                session,
                context=job_env["context"],
                draft_id=draft_id,
                expected_revision=revision,
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        identities = list(workers.map(lambda _: freeze(), range(2)))
    assert identities[0] == identities[1]
    with Session(native.engine) as session:
        result = previews.get_preview_summary(
            session, context=job_env["context"], preview_id=identities[0]
        )
        payload = result.native_request
        assert (
            native_request_digest(dict(reversed(list(payload.items()))))
            == result.content_digest
        )
        changed = copy.deepcopy(payload)
        changed["ads"][0]["text"] += " changed"
        assert native_request_digest(changed) != result.content_digest
        assert (
            len(
                session.exec(
                    select(BuildPreview).where(BuildPreview.draft_id == draft_id)
                ).all()
            )
            == 1
        )


def test_seeded_structure_properties_use_candidate_pool_without_cartesian_product():
    from types import SimpleNamespace
    from uuid import UUID

    from app.modules.strategies.copy_pool import CopyChoice, seed_copies
    from app.modules.strategies.structure import plan_structure
    from tests.modules.strategies.test_versions import config

    pool = seed_copies()[:6]
    noisy = pool + (pool[0], CopyChoice(UUID(int=1), "   "))
    for count in range(1, 51):
        units = [SimpleNamespace(unit_id=UUID(int=i + 1)) for i in range(count)]
        for versions in (1, 3, 6):
            cfg = config(
                group_generation_mode="FIXED",
                group_count=1,
                group_material_allocation="SHARED",
                max_materials_per_group=None,
                ads_per_group=1,
                creative_count=versions,
            )
            first = plan_structure(units, config=cfg, pool=noisy, seed=42)
            assert first == plan_structure(units, config=cfg, pool=noisy, seed=42)
            assert len(first) == 1 and len(first[0].ads) == 1
            ad = first[0].ads[0]
            assert len(ad.unit_ids) == count and len(ad.copies) == versions
            assert len({x.text for x in ad.copies}) == versions
            assert {x.text for x in ad.copies} <= {x.text for x in pool}
    with pytest.raises(DomainError, match="copy_pool_exhausted"):
        plan_structure(
            units,
            config=cfg.model_copy(update={"creative_count": 7}),
            pool=noisy,
            seed=42,
        )


@pytest.mark.parametrize("stale", [False, True])
def test_directory_coverage_retains_published_query_and_age(
    job_env, wire, redis_client, stale
):
    from datetime import timedelta

    from app.modules.ads.sync_models import AdDirectoryRun

    draft_id, _, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    observed = datetime.now(UTC) - timedelta(days=2 if stale else 0)
    with Session(native.engine, expire_on_commit=False) as session, session.begin():
        session.add(
            AdDirectoryRun(
                tenant_id=job_env["context"].tenant_id,
                bc_id=job_env["bc_id"],
                advertiser_id="actual-account",
                actor_id=job_env["context"].actor_id,
                connection_id=job_env["connection_id"],
                channel="OFFICIAL_API",
                frozen_route=job_env["route"].model_dump(mode="json"),
                kind="adgroup",
                ad_type="SMART_PLUS",
                partition_key="a" * 64,
                query={"ids": ["visible-group"]},
                status="COMPLETE",
                coverage="COMPLETE_MATERIALS",
                published_version=7,
                observed_at=observed,
            )
        )
        session.flush()
        draft = session.get(BuildDraft, draft_id)
        pid = previews.generate_preview(
            session,
            context=job_env["context"],
            draft_id=draft_id,
            expected_revision=draft.revision,
        )
        result = previews.get_preview_summary(
            session, context=job_env["context"], preview_id=pid
        )
        observation = result.native_request["occupancy"]
        assert observation["coverage"] == ("STALE" if stale else "PARTIAL")
        assert observation["published_version"] == 7
        assert datetime.fromisoformat(observation["observed_at"]) == observed
        assert observation["directory_query"] == {"ids": ["visible-group"]}
        assert observation["directory_coverage"] == "COMPLETE_MATERIALS"
