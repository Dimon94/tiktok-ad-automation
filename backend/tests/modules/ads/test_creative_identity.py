"""创意身份在真实 PostgreSQL 中持久化，不用消耗事实反推素材。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import inspect
from sqlmodel import select

from app.integrations.tiktok.contracts.ads import (
    AdMaterialUsage,
    EntityRef,
    MaterialUseRef,
)
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.creative_models import AdCreative
from app.modules.ads.creatives import backfill_creatives, upsert_creatives
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync import publish_directory, stage_directory_page
from tests.modules.ads.test_directory_sync import _authority, _run
from tests.modules.ads.test_material_directory_children import _source_page
from tests.modules.reporting.sync_conftest import committed_sync_seed  # noqa: F401


def _row(creative="creative", *, parent="ad", assets=None, **values):
    return {
        "creative_id": creative,
        "ad_remote_id": parent,
        "ad_material_id": "material",
        "assets": assets
        if assets is not None
        else [{"platform_material_id": "video", "material_type": "VIDEO"}],
        "name": "source",
        "operation_status": "ENABLE",
        **values,
    }


def _write(session, seed, rows, *, version=1, observed=None):
    return upsert_creatives(
        session,
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        rows=rows,
        observed_at=observed or datetime(2026, 11, 1, tzinfo=UTC),
        published_version=version,
    )


def _get(session, seed, creative="creative"):
    session.expire_all()
    return session.get(
        AdCreative, (seed.context.tenant_id, seed.ref.advertiser_id, creative)
    )


def test_migration_has_account_scope_and_material_indexes(session):
    schema = inspect(session.connection())
    assert schema.get_pk_constraint("ad_creative")["constrained_columns"] == [
        "tenant_id",
        "advertiser_id",
        "creative_id",
    ]
    assert any(
        fk["referred_table"] == "advertiser_account"
        for fk in schema.get_foreign_keys("ad_creative")
    )
    indexes = {
        tuple(index["column_names"]) for index in schema.get_indexes("ad_creative")
    }
    assert ("tenant_id", "platform_material_id", "advertiser_id") in indexes
    assert ("tenant_id", "advertiser_id", "ad_remote_id") in indexes


@pytest.mark.parametrize(
    ("assets", "kind", "status", "platform"),
    [
        (
            [{"platform_material_id": "video", "material_type": "VIDEO"}],
            "VIDEO",
            "RESOLVED",
            "video",
        ),
        (
            [{"platform_material_id": "image", "material_type": "IMAGE"}],
            "IMAGE",
            "RESOLVED",
            "image",
        ),
        (
            [{"platform_material_id": "post", "material_type": "TIKTOK_POST"}],
            "TIKTOK_POST",
            "RESOLVED",
            "post",
        ),
        (
            [
                {"platform_material_id": "b", "material_type": "IMAGE"},
                {"platform_material_id": "a", "material_type": "IMAGE"},
            ],
            "CAROUSEL",
            "COMPOSITE",
            None,
        ),
        ([], None, "UNRESOLVED", None),
    ],
)
def test_resolves_assets_without_assigning_carousel_to_first_image(
    session, directory_seed, assets, kind, status, platform
):
    assert _write(session, directory_seed, [_row(assets=assets)]) == 1
    row = _get(session, directory_seed)
    assert (row.material_type, row.identity_status, row.platform_material_id) == (
        kind,
        status,
        platform,
    )
    assert row.assets == assets


def test_shared_video_has_separate_creatives_and_no_absence_delete(
    session, directory_seed
):
    _write(session, directory_seed, [_row("one"), _row("two", parent="other")])
    _write(
        session, directory_seed, [_row("one", operation_status="DISABLE")], version=2
    )
    _write(session, directory_seed, [], version=3)
    assert _get(session, directory_seed, "two").deleted is False
    assert _get(session, directory_seed, "one").operation_status == "DISABLE"


def test_old_observation_and_old_version_cannot_overwrite(session, directory_seed):
    now = datetime.now(UTC)
    _write(session, directory_seed, [_row()], version=10, observed=now)
    for version, stamp in [(9, now + timedelta(days=1)), (11, now - timedelta(days=1))]:
        assert (
            _write(
                session,
                directory_seed,
                [_row(name="stale")],
                version=version,
                observed=stamp,
            )
            == 0
        )
    assert _get(session, directory_seed).name == "source"


def test_identity_conflict_records_evidence_and_keeps_original(session, directory_seed):
    _write(session, directory_seed, [_row()])
    _write(
        session,
        directory_seed,
        [
            _row(
                parent="wrong",
                assets=[{"platform_material_id": "other", "material_type": "VIDEO"}],
            )
        ],
        version=2,
    )
    row = _get(session, directory_seed)
    assert row.identity_status == "CONFLICT"
    assert row.ad_remote_id == "ad" and row.platform_material_id == "video"
    assert row.evidence["original_identity"]["ad_remote_id"] == "ad"
    assert row.evidence["conflicts"][0]["identity"]["ad_remote_id"] == "wrong"
    _write(session, directory_seed, [_row()], version=3)
    assert _get(session, directory_seed).identity_status == "CONFLICT"


def test_explicit_delete_retains_identity(session, directory_seed):
    _write(session, directory_seed, [_row()])
    _write(
        session,
        directory_seed,
        [_row(deleted=True, operation_status="DELETE")],
        version=2,
    )
    row = _get(session, directory_seed)
    assert row.deleted and row.platform_material_id == "video"
    assert row.first_observed_at < row.observed_at or row.published_version == 2


def test_ids_isolated_by_tenant_and_account(session, directory_seed, other_context):
    _write(session, directory_seed, [_row()])
    for tenant, account in [
        (directory_seed.context.tenant_id, "other"),
        (other_context.tenant_id, directory_seed.ref.advertiser_id),
    ]:
        session.add(
            AdvertiserAccount(
                tenant_id=tenant, advertiser_id=account, currency="USD", timezone="UTC"
            )
        )
        session.flush()
        upsert_creatives(
            session,
            tenant_id=tenant,
            advertiser_id=account,
            rows=[_row(parent="different")],
            observed_at=datetime.now(UTC),
            published_version=2,
        )
    assert _get(session, directory_seed).ad_remote_id == "ad"


def test_backfill_preserves_existing_complete_identity_without_legacy_references(
    session, directory_seed
):
    owner = directory_seed.row
    assets = [
        {"platform_material_id": image, "material_type": "IMAGE"}
        for image in ["b", "a"]
    ]
    _write(
        session,
        directory_seed,
        [_row("carousel", parent=owner.remote_id, assets=assets)],
        version=999,
    )
    backfill_creatives(
        session, tenant_id=owner.tenant_id, advertiser_id=owner.advertiser_id
    )
    carousel = _get(session, directory_seed, "carousel")
    assert carousel.identity_status == "COMPOSITE" and carousel.assets == assets
    assert carousel.published_version == 999


@pytest.mark.parametrize("smart", [False, True])
def test_directory_publish_persists_normal_and_native_identities(
    session, directory_seed, smart
):
    _authority(session, directory_seed)
    run = _run(
        session, directory_seed, kind="ad", ad_type="SMART_PLUS" if smart else "REGULAR"
    )
    source = _source_page(directory_seed, 1, [("ad", "group")], terminal=True)
    owner = replace(source.items[0], ad_type=run.ad_type)
    uses = tuple(
        AdMaterialUsage(
            MaterialUseRef(owner.ref, image, "images" if smart else None, "IMAGE"),
            None,
            "ENABLE",
            True,
            creative_ids=("native",) if smart else (),
        )
        for image in ["b", "a"]
    )
    stage_directory_page(
        session,
        run_id=run.id,
        page=replace(source, items=(owner,), materials=uses),
        claim_generation=1,
    )
    version = publish_directory(session, run_id=run.id, claim_generation=1)
    creative = _get(session, directory_seed, "native" if smart else "ad")
    assert creative is not None and creative.published_version == version
    assert creative.assets == [
        {"platform_material_id": image, "material_type": "IMAGE"}
        for image in ["b", "a"]
    ]
    assert creative.material_type == "CAROUSEL"
    assert (
        len(
            session.exec(
                select(AdCreative).where(
                    AdCreative.tenant_id == directory_seed.context.tenant_id
                )
            ).all()
        )
        == 1
    )


def test_concurrent_stale_publication_cannot_replace_new_identity(committed_sync_seed):  # noqa: F811
    seed, factory = committed_sync_seed
    started = Event()
    now = datetime.now(UTC)

    def stale_write():
        with factory() as db:
            started.set()
            count = upsert_creatives(
                db,
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                rows=[_row(parent="stale")],
                observed_at=now - timedelta(hours=1),
                published_version=1,
            )
            db.commit()
            return count

    with ThreadPoolExecutor(max_workers=1) as pool, factory() as winner:
        upsert_creatives(
            winner,
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            rows=[_row()],
            observed_at=now,
            published_version=2,
        )
        pending = pool.submit(stale_write)
        assert started.wait(5)
        winner.commit()
        assert pending.result(timeout=5) == 0
    with factory() as db:
        row = db.get(AdCreative, (seed.tenant_id, "acc-1", "creative"))
        assert row.ad_remote_id == "ad" and row.identity_status == "RESOLVED"


def test_normal_creative_child_and_partial_delete_preserve_assets(
    session, directory_seed
):
    _authority(session, directory_seed)
    owner = directory_seed.row
    source = _source_page(
        directory_seed, 1, [(owner.remote_id, "group")], terminal=True
    )
    child = replace(
        source.items[0],
        ref=EntityRef(owner.tenant_id, owner.advertiser_id, "creative", "native"),
        parent_ref=owner.ref,
        ad_type="REGULAR",
    )
    usage = AdMaterialUsage(
        MaterialUseRef(owner.ref, "post", None, "TIKTOK_POST"),
        None,
        "ENABLE",
        True,
        creative_ids=("native",),
    )
    for status, materials in [("ENABLE", (usage,)), ("DELETE", ())]:
        run = _run(session, directory_seed, kind="creative", ad_type="REGULAR")
        child = replace(
            child,
            operation_status=status,
            observed_at=child.observed_at + timedelta(seconds=1),
        )
        stage_directory_page(
            session,
            run_id=run.id,
            page=replace(
                source,
                items=(child,),
                materials=materials,
                materials_complete=bool(materials),
                material_missing_reason=None if materials else "partial",
            ),
            claim_generation=1,
        )
        publish_directory(session, run_id=run.id, claim_generation=1)
    row = _get(session, directory_seed, "native")
    assert row.deleted and row.platform_material_id == "post"
    assert row.identity_status == "RESOLVED"


def test_deleted_smart_parent_retains_and_deletes_historical_creatives(
    session, directory_seed
):
    _authority(session, directory_seed)
    owner = directory_seed.row
    _write(
        session,
        directory_seed,
        [_row("native", parent=owner.remote_id)],
        observed=owner.observed_at,
    )
    source = _source_page(
        directory_seed, 1, [(owner.remote_id, "group")], terminal=True
    )
    deleted = replace(
        source.items[0],
        operation_status="DELETE",
        observed_at=owner.observed_at + timedelta(seconds=1),
    )
    run = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=run.id,
        page=replace(source, items=(deleted,), materials=(), materials_complete=False),
        claim_generation=1,
    )
    publish_directory(session, run_id=run.id, claim_generation=1)
    row = _get(session, directory_seed, "native")
    assert row.deleted and row.platform_material_id == "video"


def test_backfill_normal_ad_uses_official_snapshot_without_native_id(
    session, directory_seed
):
    owner = directory_seed.row
    owner.ad_type = "REGULAR"
    owner.configuration = {"video_id": "video"}
    session.add(owner)
    session.flush()
    backfill_creatives(
        session, tenant_id=owner.tenant_id, advertiser_id=owner.advertiser_id
    )
    assert (
        _get(session, directory_seed, owner.remote_id).platform_material_id == "video"
    )


@pytest.mark.parametrize("original_material", [None, "material"])
def test_empty_unresolved_identity_can_be_enriched_without_losing_original_evidence(
    session, directory_seed, original_material
):
    _write(session, directory_seed, [_row(assets=[], ad_material_id=original_material)])
    _write(session, directory_seed, [_row()], version=2)
    row = _get(session, directory_seed)
    assert row.platform_material_id == "video" and row.identity_status == "RESOLVED"
    assert row.evidence["original_identity"]["assets"] == []
    assert row.evidence["enrichments"][0]["identity"]["assets"] == row.assets
    _write(session, directory_seed, [_row(name="latest")], version=3)
    assert _get(session, directory_seed).identity_status == "RESOLVED"


def test_empty_unresolved_identity_cannot_enrich_a_different_material_id(
    session, directory_seed
):
    _write(session, directory_seed, [_row(assets=[], ad_material_id="original")])
    _write(session, directory_seed, [_row()], version=2)
    row = _get(session, directory_seed)
    assert (
        row.identity_status == "CONFLICT"
        and row.assets == []
        and row.ad_material_id == "original"
    )


def test_duplicate_asset_across_batches_preserves_first_observed_carousel_order(
    session, directory_seed, monkeypatch
):
    from app.modules.ads import sync

    monkeypatch.setattr(sync, "_PUBLISH_BATCH_SIZE", 1)
    _authority(session, directory_seed)
    source = _source_page(directory_seed, 1, [("ad", "group")], terminal=True)
    owner = source.items[0]
    uses = tuple(
        AdMaterialUsage(
            MaterialUseRef(owner.ref, image, "images", "IMAGE"),
            None,
            "ENABLE",
            True,
            creative_ids=("native",),
        )
        for image in ["b", "a", "b"]
    )
    run = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session, run_id=run.id, page=replace(source, materials=uses), claim_generation=1
    )
    publish_directory(session, run_id=run.id, claim_generation=1)
    assert [
        asset["platform_material_id"]
        for asset in _get(session, directory_seed, "native").assets
    ] == ["b", "a"]


def test_backfill_reuses_known_order_instead_of_conflicting_with_unordered_history(
    session, directory_seed
):
    owner = directory_seed.row
    assets = [
        {"platform_material_id": image, "material_type": "IMAGE"}
        for image in ["b", "a"]
    ]
    _write(
        session,
        directory_seed,
        [_row(parent=owner.remote_id, assets=assets)],
        observed=owner.observed_at,
    )
    for asset in assets:
        session.add(
            AdMaterialReference(
                tenant_id=owner.tenant_id,
                advertiser_id=owner.advertiser_id,
                ad_remote_id=owner.remote_id,
                ad_material_id="material",
                complete=True,
                published_version=1,
                **asset,
            )
        )
    session.flush()
    backfill_creatives(
        session, tenant_id=owner.tenant_id, advertiser_id=owner.advertiser_id
    )
    row = _get(session, directory_seed)
    assert row.identity_status == "COMPOSITE" and row.assets == assets


@pytest.mark.parametrize("batch_size", [1, 500])
@pytest.mark.parametrize(
    "asset_lists,asset_type",
    [
        ((["shared"], ["shared"]), "VIDEO"),
        ((["shared", "first-only"], ["second-only", "shared"]), "IMAGE"),
    ],
)
def test_cross_page_shared_assets_retain_every_creative_membership_and_order(
    session, directory_seed, monkeypatch, batch_size, asset_lists, asset_type
):
    from app.modules.ads import sync

    monkeypatch.setattr(sync, "_PUBLISH_BATCH_SIZE", batch_size)
    _authority(session, directory_seed)
    owner = directory_seed.row
    run = _run(session, directory_seed, kind="creative", ad_type="REGULAR")
    for number, asset_ids in enumerate(asset_lists, 1):
        source = _source_page(
            directory_seed, number, [(owner.remote_id, "group")], terminal=number == 2
        )
        child = replace(
            source.items[0],
            ref=EntityRef(
                owner.tenant_id, owner.advertiser_id, "creative", f"creative-{number}"
            ),
            parent_ref=owner.ref,
            ad_type="REGULAR",
        )
        usages = tuple(
            AdMaterialUsage(
                MaterialUseRef(owner.ref, asset, None, asset_type),
                None,
                "ENABLE",
                True,
                creative_ids=(child.ref.remote_id,),
            )
            for asset in asset_ids
        )
        stage_directory_page(
            session,
            run_id=run.id,
            page=replace(
                source,
                items=(child,),
                materials=usages,
                materials_complete=True,
                material_missing_reason=None,
            ),
            claim_generation=1,
        )
    publish_directory(session, run_id=run.id, claim_generation=1)
    for number, asset_ids in enumerate(asset_lists, 1):
        creative = _get(session, directory_seed, f"creative-{number}")
        assert creative.identity_status == (
            "RESOLVED" if asset_type == "VIDEO" else "COMPOSITE"
        )
        assert creative.assets == [
            {"platform_material_id": asset, "material_type": asset_type}
            for asset in asset_ids
        ]
        assert creative.platform_material_id == (
            "shared" if asset_type == "VIDEO" else None
        )


def test_newer_partial_active_child_cannot_revive_explicitly_deleted_parent(
    session, directory_seed
):
    _authority(session, directory_seed)
    owner = directory_seed.row
    source = _source_page(
        directory_seed, 1, [(owner.remote_id, "group")], terminal=True
    )
    observation = owner.observed_at
    parent_ref = owner.ref
    _write(
        session,
        directory_seed,
        [_row("native", parent=parent_ref.remote_id)],
        observed=observation,
    )

    def publish_parent(status, offset):
        parent = replace(
            source.items[0],
            operation_status=status,
            observed_at=observation + timedelta(seconds=offset),
        )
        run = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
        stage_directory_page(
            session,
            run_id=run.id,
            page=replace(source, items=(parent,), materials=()),
            claim_generation=1,
        )
        publish_directory(session, run_id=run.id, claim_generation=1)

    def publish_partial_child(offset):
        child = replace(
            source.items[0],
            ref=EntityRef(
                parent_ref.tenant_id, parent_ref.advertiser_id, "creative", "native"
            ),
            parent_ref=parent_ref,
            ad_type="REGULAR",
            operation_status="ENABLE",
            observed_at=observation + timedelta(seconds=offset),
        )
        run = _run(session, directory_seed, kind="creative", ad_type="REGULAR")
        stage_directory_page(
            session,
            run_id=run.id,
            page=replace(source, items=(child,), materials=()),
            claim_generation=1,
        )
        publish_directory(session, run_id=run.id, claim_generation=1)

    publish_parent("DELETE", 1)
    assert _get(session, directory_seed, "native").deleted
    publish_partial_child(2)
    creative = _get(session, directory_seed, "native")
    assert creative.deleted and creative.operation_status == "ENABLE"
    assert creative.platform_material_id == "video"
    # 只有父目录的明确新观察才允许之后的活跃子观察恢复有效状态。
    publish_parent("ENABLE", 3)
    publish_partial_child(4)
    assert not _get(session, directory_seed, "native").deleted


@pytest.mark.parametrize("reference_version", [4, 5])
@pytest.mark.parametrize(
    "live_mid,reference_mid",
    [("material", "material"), ("material", None), (None, "material")],
)
def test_same_or_older_backfill_does_not_overwrite_live_status(
    session, directory_seed, reference_version, live_mid, reference_mid
):
    owner = directory_seed.row
    observed = owner.observed_at
    _write(
        session,
        directory_seed,
        [
            _row(
                parent=owner.remote_id,
                name="live",
                operation_status="DISABLE",
                ad_material_id=live_mid,
            )
        ],
        version=5,
        observed=observed,
    )
    owner.observed_at = observed + timedelta(seconds=10)
    session.add(
        AdMaterialReference(
            tenant_id=owner.tenant_id,
            advertiser_id=owner.advertiser_id,
            ad_remote_id=owner.remote_id,
            platform_material_id="video",
            material_type="VIDEO",
            ad_material_id=reference_mid,
            complete=True,
            name="historical",
            operation_status="ENABLE",
            published_version=reference_version,
        )
    )
    session.flush()
    backfill_creatives(
        session, tenant_id=owner.tenant_id, advertiser_id=owner.advertiser_id
    )
    creative = _get(session, directory_seed)
    assert (
        creative.name,
        creative.operation_status,
        creative.observed_at,
        creative.published_version,
        creative.ad_material_id,
    ) == ("live", "DISABLE", observed, 5, live_mid)


def test_partial_backfill_preserves_complete_live_carousel(session, directory_seed):
    owner = directory_seed.row
    assets = [
        {"platform_material_id": image, "material_type": "IMAGE"}
        for image in ["b", "a"]
    ]
    _write(
        session,
        directory_seed,
        [_row(parent=owner.remote_id, assets=assets)],
        observed=owner.observed_at,
    )
    owner.observed_at += timedelta(seconds=1)
    # canonical共享素材成员曾被后页覆盖；更高版本也不能把缺失解释为资产删除。
    session.add(
        AdMaterialReference(
            tenant_id=owner.tenant_id,
            advertiser_id=owner.advertiser_id,
            ad_remote_id=owner.remote_id,
            platform_material_id="a",
            material_type="IMAGE",
            ad_material_id="material",
            complete=True,
            published_version=2,
        )
    )
    session.flush()
    backfill_creatives(
        session, tenant_id=owner.tenant_id, advertiser_id=owner.advertiser_id
    )
    creative = _get(session, directory_seed)
    assert creative.identity_status == "COMPOSITE" and creative.assets == assets
    assert creative.evidence["conflicts"] == []


@pytest.mark.parametrize("changed", ["parent", "ad_material", "asset"])
def test_backfill_still_records_genuinely_conflicting_identity(
    session, directory_seed, changed
):
    owner = directory_seed.row
    _write(
        session,
        directory_seed,
        [_row(parent=owner.remote_id)],
        observed=owner.observed_at,
    )
    child = _cached_child(
        session,
        directory_seed,
        creative="creative",
        configuration={
            "video_id": "different-video" if changed == "asset" else "video",
            "ad_material_id": "different-material"
            if changed == "ad_material"
            else "material",
        },
    )
    child.observed_at += timedelta(seconds=1)
    if changed == "parent":
        child.parent_remote_id = "different-parent"
    session.add(child)
    session.flush()
    backfill_creatives(
        session, tenant_id=owner.tenant_id, advertiser_id=owner.advertiser_id
    )
    creative = _get(session, directory_seed)
    assert creative.identity_status == "CONFLICT"
    assert (
        creative.ad_remote_id == owner.remote_id
        and creative.platform_material_id == "video"
    )


@pytest.mark.parametrize("first,second", [("material", None), (None, "material")])
def test_missing_mid_does_not_conflict_with_same_parent_and_video(
    session, directory_seed, first, second
):
    _write(session, directory_seed, [_row(ad_material_id=first)])
    _write(session, directory_seed, [_row(ad_material_id=second)], version=2)
    row = _get(session, directory_seed)
    assert row.identity_status == "RESOLVED"
    assert row.ad_material_id == "material"
    assert row.assets == [{"platform_material_id": "video", "material_type": "VIDEO"}]
    assert row.evidence["conflicts"] == []
    assert row.evidence["original_identity"]["ad_material_id"] == first
    if first is None:
        assert (
            row.evidence["enrichments"][0]["identity"]["ad_material_id"] == "material"
        )


def test_different_known_mid_remains_a_conflict(session, directory_seed):
    _write(session, directory_seed, [_row(ad_material_id="original")])
    _write(session, directory_seed, [_row(ad_material_id="different")], version=2)
    _write(session, directory_seed, [_row(ad_material_id=None)], version=3)
    row = _get(session, directory_seed)
    assert row.identity_status == "CONFLICT"
    assert row.ad_material_id == "original"
    assert row.evidence["conflicts"][0]["identity"]["ad_material_id"] == "different"


@pytest.mark.parametrize("real_conflict", [False, True])
def test_new_observation_reclassifies_only_old_missing_mid_conflicts(
    session, directory_seed, real_conflict
):
    _write(session, directory_seed, [_row()])
    row = _get(session, directory_seed)
    identity = dict(row.evidence["original_identity"], ad_material_id=None)
    conflicts = [{"identity": identity, "published_version": 1}]
    if real_conflict:
        conflicts.append({"identity": dict(identity, ad_material_id="different")})
    row.identity_status = "CONFLICT"
    row.evidence = dict(row.evidence, conflicts=conflicts)
    session.add(row)
    session.flush()
    _write(session, directory_seed, [_row()], version=2)
    row = _get(session, directory_seed)
    assert row.identity_status == ("CONFLICT" if real_conflict else "RESOLVED")
    assert row.evidence["conflicts"] == conflicts
    if not real_conflict:
        assert (
            row.evidence["conflict_recoveries"][0]["reason"] == "missing_ad_material_id"
        )


@pytest.mark.parametrize("targets", [["wanted"], []])
def test_prioritized_backfill_only_writes_requested_creatives_with_all_assets(
    session, directory_seed, targets
):
    owner = directory_seed.row
    for cid in ["wanted", "other"]:
        _cached_child(
            session,
            directory_seed,
            creative=cid,
            configuration={"image_ids": ["a", "b"], "ad_material_id": "carousel"},
        )
    backfill_creatives(
        session,
        tenant_id=owner.tenant_id,
        advertiser_id=owner.advertiser_id,
        creative_ids=targets,
    )
    assert _get(session, directory_seed, "other") is None
    wanted = _get(session, directory_seed, "wanted")
    if targets:
        assert wanted.identity_status == "COMPOSITE"
        assert wanted.assets == [
            {"platform_material_id": p, "material_type": "IMAGE"} for p in ["a", "b"]
        ]
    else:
        assert wanted is None


def _cached_child(session, seed, *, creative="cached", configuration=None, version=2):
    row = AdObject(
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        kind="creative",
        remote_id=creative,
        parent_kind="ad",
        parent_remote_id=seed.row.remote_id,
        ad_type="SMART_PLUS",
        name="cached child",
        configuration=configuration or {},
        operation_status="ENABLE",
        observed_at=seed.row.observed_at,
        published_version=version,
    )
    session.add(row)
    session.flush()
    return row


def test_backfill_reads_cached_creative_video_when_reference_was_lost(
    session, directory_seed
):
    child = _cached_child(
        session, directory_seed, configuration={"video_id": "cached-video"}
    )
    backfill_creatives(
        session,
        tenant_id=child.tenant_id,
        advertiser_id=child.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert row is not None
    assert (row.ad_remote_id, row.platform_material_id, row.identity_status) == (
        directory_seed.row.remote_id,
        "cached-video",
        "RESOLVED",
    )
    assert row.observed_at == child.observed_at and row.published_version == 2


def test_backfill_reads_parent_cached_creative_list_and_preserves_image_order(
    session, directory_seed
):
    parent = directory_seed.row
    parent.configuration = {
        "creative_list": [
            {
                "smart_plus_creative_id": "cached-carousel",
                "ad_material_id": "mid",
                "creative_info": {"image_info": [{"image_id": "b"}, {"image_id": "a"}]},
            }
        ]
    }
    session.add(parent)
    session.flush()
    backfill_creatives(
        session,
        tenant_id=parent.tenant_id,
        advertiser_id=parent.advertiser_id,
        creative_ids=["cached-carousel"],
    )
    row = _get(session, directory_seed, "cached-carousel")
    assert row is not None and row.identity_status == "COMPOSITE"
    assert row.ad_material_id == "mid"
    assert row.assets == [
        {"material_type": "IMAGE", "platform_material_id": "b"},
        {"material_type": "IMAGE", "platform_material_id": "a"},
    ]


def test_cached_empty_child_retains_older_positive_reference_and_parent_delete(
    session, directory_seed
):
    owner = directory_seed.row
    child = _cached_child(session, directory_seed)
    owner.operation_status = "DELETE"
    session.add(owner)
    _write(
        session,
        directory_seed,
        [
            _row(
                "cached",
                parent=owner.remote_id,
                assets=[
                    {"platform_material_id": "known-video", "material_type": "VIDEO"}
                ],
            )
        ],
        observed=owner.observed_at,
        version=1,
    )
    session.flush()
    backfill_creatives(
        session,
        tenant_id=owner.tenant_id,
        advertiser_id=owner.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert (
        row.platform_material_id == "known-video" and row.identity_status == "RESOLVED"
    )
    assert row.deleted and row.published_version == child.published_version


def test_cached_reference_absence_creates_unknown_and_never_uses_foreign_raw_asset(
    session, directory_seed
):
    child = _cached_child(
        session,
        directory_seed,
        configuration={"advertiser_id": "foreign-account", "video_id": "foreign-video"},
    )
    backfill_creatives(
        session,
        tenant_id=child.tenant_id,
        advertiser_id=child.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert row is not None and row.identity_status == "UNRESOLVED"
    assert row.assets == [] and row.platform_material_id is None


def test_cached_same_version_backfill_cannot_overwrite_live_metadata(
    session, directory_seed
):
    child = _cached_child(session, directory_seed, configuration={"video_id": "video"})
    _write(
        session,
        directory_seed,
        [
            _row(
                "cached",
                parent=child.parent_remote_id,
                ad_material_id=None,
                name="live",
                operation_status="DISABLE",
            )
        ],
        version=2,
        observed=child.observed_at,
    )
    child.observed_at += timedelta(seconds=10)
    session.add(child)
    session.flush()
    backfill_creatives(
        session,
        tenant_id=child.tenant_id,
        advertiser_id=child.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert row.name == "live" and row.operation_status == "DISABLE"
    assert row.observed_at == child.observed_at - timedelta(seconds=10)


@pytest.mark.parametrize("batch_size", [1, 500])
def test_cached_post_then_empty_child_does_not_depend_on_batch_boundary(
    session, directory_seed, monkeypatch, batch_size
):
    from app.modules.ads import creatives

    monkeypatch.setattr(creatives, "_BATCH_SIZE", batch_size)
    parent = directory_seed.row
    parent.configuration = {
        "creative_list": [
            {
                "smart_plus_creative_id": "cached",
                "ad_material_id": "post-mid",
                "creative_info": {"tiktok_item_id": "post"},
            }
        ]
    }
    session.add(parent)
    _cached_child(session, directory_seed)
    backfill_creatives(
        session,
        tenant_id=parent.tenant_id,
        advertiser_id=parent.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert (
        row.identity_status,
        row.material_type,
        row.platform_material_id,
        row.ad_material_id,
        row.published_version,
    ) == ("RESOLVED", "TIKTOK_POST", "post", "post-mid", 2)
    assert row.evidence["conflicts"] == []


def test_cached_partial_carousel_never_becomes_a_resolved_single_image(
    session, directory_seed
):
    parent = directory_seed.row
    parent.configuration = {
        "creative_list": [
            {
                "smart_plus_creative_id": "cached",
                "creative_info": {"image_info": [{"image_id": "a"}, {}]},
            }
        ]
    }
    session.add(parent)
    session.flush()
    backfill_creatives(
        session,
        tenant_id=parent.tenant_id,
        advertiser_id=parent.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert row.identity_status == "UNRESOLVED" and row.assets == []
    assert row.platform_material_id is None


def test_cached_post_cannot_hide_a_different_known_mid(session, directory_seed):
    child = _cached_child(
        session,
        directory_seed,
        configuration={"tiktok_item_id": "post", "ad_material_id": "different"},
    )
    _write(
        session,
        directory_seed,
        [_row("cached", parent=child.parent_remote_id, ad_material_id="original")],
        observed=child.observed_at,
    )
    backfill_creatives(
        session,
        tenant_id=child.tenant_id,
        advertiser_id=child.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert row.identity_status == "CONFLICT" and row.ad_material_id == "original"
    conflict = row.evidence["conflicts"][0]["identity"]
    assert conflict["ad_material_id"] == "different"
    assert conflict["assets"] == [
        {"platform_material_id": "post", "material_type": "TIKTOK_POST"}
    ]


def test_partial_cached_images_with_a_new_member_keep_a_real_conflict(
    session, directory_seed
):
    child = _cached_child(
        session, directory_seed, configuration={"image_ids": ["a", None, "c"]}
    )
    _write(
        session,
        directory_seed,
        [
            _row(
                "cached",
                parent=child.parent_remote_id,
                assets=[
                    {"material_type": "IMAGE", "platform_material_id": "a"},
                    {"material_type": "IMAGE", "platform_material_id": "b"},
                ],
            )
        ],
        observed=child.observed_at,
    )
    backfill_creatives(
        session,
        tenant_id=child.tenant_id,
        advertiser_id=child.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert row.identity_status == "CONFLICT"
    assert [a["platform_material_id"] for a in row.assets] == ["a", "b"]


def test_cached_empty_content_preserves_explicit_mid_conflict(session, directory_seed):
    child = _cached_child(
        session, directory_seed, configuration={"ad_material_id": "different"}
    )
    _write(
        session,
        directory_seed,
        [_row("cached", parent=child.parent_remote_id, ad_material_id="original")],
        observed=child.observed_at,
    )
    backfill_creatives(
        session,
        tenant_id=child.tenant_id,
        advertiser_id=child.advertiser_id,
        creative_ids=["cached"],
    )
    row = _get(session, directory_seed, "cached")
    assert row.identity_status == "CONFLICT" and row.ad_material_id == "original"
    assert row.evidence["conflicts"][0]["identity"]["ad_material_id"] == "different"


def test_cached_unknown_identity_keeps_mid_before_later_asset_observation(
    session, directory_seed
):
    child = _cached_child(
        session, directory_seed, configuration={"ad_material_id": "original"}
    )
    args = {
        "tenant_id": child.tenant_id,
        "advertiser_id": child.advertiser_id,
        "creative_ids": ["cached"],
    }
    backfill_creatives(session, **args)
    assert _get(session, directory_seed, "cached").ad_material_id == "original"
    child.configuration = {"video_id": "video", "ad_material_id": "different"}
    child.published_version = 3
    child.observed_at += timedelta(seconds=1)
    session.add(child)
    session.flush()
    backfill_creatives(session, **args)
    assert _get(session, directory_seed, "cached").identity_status == "CONFLICT"


@pytest.mark.parametrize(
    "configuration,expected_assets,expected_status",
    [
        (
            {"video_id": "cached-video"},
            [{"platform_material_id": "cached-video", "material_type": "VIDEO"}],
            "RESOLVED",
        ),
        (
            {"image_ids": ["b", "a"]},
            [{"platform_material_id": p, "material_type": "IMAGE"} for p in ["b", "a"]],
            "COMPOSITE",
        ),
        ({"advertiser_id": "foreign", "video_id": "foreign-video"}, [], "UNRESOLVED"),
        ({}, [], "UNRESOLVED"),
    ],
)
def test_live_child_publication_uses_explicit_cached_assets_without_material_references(
    session, directory_seed, configuration, expected_assets, expected_status
):
    _authority(session, directory_seed)
    parent = directory_seed.row.ref
    source = _source_page(
        directory_seed, 1, [(parent.remote_id, "group")], terminal=True
    )
    child = replace(
        source.items[0],
        ref=EntityRef(parent.tenant_id, parent.advertiser_id, "creative", "native"),
        parent_ref=parent,
        ad_type="REGULAR",
        configuration=configuration,
    )
    run = _run(session, directory_seed, kind="creative", ad_type="REGULAR")
    stage_directory_page(
        session,
        run_id=run.id,
        page=replace(source, items=(child,), materials=()),
        claim_generation=1,
    )
    publish_directory(session, run_id=run.id, claim_generation=1)
    row = _get(session, directory_seed, "native")
    assert row.identity_status == expected_status
    assert row.assets == expected_assets
    assert row.ad_remote_id == parent.remote_id
