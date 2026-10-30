"""真实主/子目录发布后归档；S3仅替换传输边界。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import Session, col, select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage, EntityRef
from app.modules.ads.models import AdObject
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.sync_control.archive import archive_run, freeze_archive
from app.modules.sync_control.archive_storage import S3RunArchive
from tests.modules.ads.test_directory_sync import _authority, _run
from tests.modules.ads.test_material_directory_children import _source_page
from tests.modules.sync_control.test_archive import ObjectTransport


def published_chain(session, seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, seed)
    source = _run(session, seed, kind="ad", ad_type="SMART_PLUS")
    page = _source_page(seed, 1, [("ad-a", "group"), ("ad-b", "group")], terminal=True)
    stage_directory_page(session, run_id=source.id, claim_generation=1, page=page)
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    assert child.material_owner_observed_through == max(
        ad.observed_at for ad in page.items
    )
    creatives = tuple(
        replace(
            ad,
            ref=EntityRef(
                ad.ref.tenant_id,
                ad.ref.advertiser_id,
                "creative",
                f"creative-{ad.ref.remote_id}",
            ),
            parent_ref=ad.ref,
        )
        for ad in page.items
    )
    stage_directory_page(
        session,
        run_id=child.id,
        claim_generation=1,
        page=DirectoryPage(
            creatives,
            tuple(replace(m, complete=True) for m in page.materials),
            None,
            True,
            CallEvidence(),
        ),
    )
    publish_directory(session, run_id=child.id, claim_generation=1)
    assert child.status == "COMPLETE" and child.published_version is not None
    return source, child


def refresh(session, seed):
    newer = _run(session, seed, kind="ad", ad_type="SMART_PLUS")
    newer.query = {**newer.query, "include_deleted": True}
    page = _source_page(seed, 1, [("ad-a", "group"), ("ad-b", "group")], terminal=True)
    page = replace(
        page,
        materials=tuple(replace(m, complete=True) for m in page.materials),
        materials_complete=True,
        material_missing_reason=None,
    )
    stage_directory_page(session, run_id=newer.id, claim_generation=1, page=page)
    publish_directory(session, run_id=newer.id, claim_generation=1)
    assert newer.status == "COMPLETE"
    return newer


def archive_context(session, source, child):
    # 模拟短期页清理已完成，所有发布/对象事实仍是真实服务产生。
    session.execute(
        delete(AdDirectoryPage).where(
            col(AdDirectoryPage.run_id).in_([source.id, child.id])
        )
    )
    now = datetime.now(UTC) + timedelta(days=91)
    connection = session.connection()
    session.commit()

    def factory():
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    transport = ObjectTransport()
    storage = S3RunArchive(transport, "private-test")

    def apply(row):
        return archive_run(
            factory,
            storage,
            tenant_id=row.tenant_id,
            kind="directory",
            run_id=row.id,
            now=now,
        )

    return now, factory, transport, apply


def test_published_child_then_primary_archive_after_complete_refresh(
    session, directory_seed, monkeypatch
):
    source, child = published_chain(session, directory_seed, monkeypatch)
    newer = refresh(session, directory_seed)
    source_id, child_id = source.id, child.id
    now, factory, transport, apply = archive_context(session, source, child)
    assert apply(source).reason == "directory_source_reference"
    assert apply(child).deleted
    assert apply(source).deleted
    with factory() as db:
        assert db.get(AdDirectoryRun, child_id) is None
        assert db.get(AdDirectoryRun, source_id) is None
        assert db.get(AdDirectoryRun, newer.id) is not None
        assert (
            len(
                db.exec(
                    select(AdObject).where(AdObject.tenant_id == source.tenant_id)
                ).all()
            )
            == 5
        )
    assert len([key for key in transport.objects if "/committed/" in key]) == 2


def test_legacy_child_preview_readonly_first_apply_defers_until_newer_observation(
    session, directory_seed, monkeypatch
):
    source, child = published_chain(session, directory_seed, monkeypatch)
    refresh(session, directory_seed)
    child.material_owner_observed_through = None
    now, factory, transport, apply = archive_context(session, source, child)
    with factory() as db:
        frozen = freeze_archive(
            db, tenant_id=child.tenant_id, kind="directory", run_id=child.id, now=now
        )
        assert frozen.keep_reason == "material_child_owner_baseline_missing"
        frozen.close()
        assert db.get(AdDirectoryRun, child.id).material_owner_observed_through is None
    assert apply(child).reason == "material_child_owner_baseline_registered"
    assert not transport.objects
    assert apply(child).reason == "material_child_owner_observation_current"
    with factory() as db:
        refresh(db, directory_seed)
        db.commit()
    assert apply(child).deleted
    assert apply(source).deleted


@pytest.mark.parametrize(
    "guard", ["owner_missing", "owner_connection", "source_route", "no_full_cover"]
)
def test_child_archive_requires_all_owners_and_frozen_authorization(
    session, directory_seed, monkeypatch, guard
):
    source, child = published_chain(session, directory_seed, monkeypatch)
    if guard != "no_full_cover":
        refresh(session, directory_seed)
    owner = session.get(
        AdObject, (source.tenant_id, source.advertiser_id, "ad", "ad-b")
    )
    if guard == "owner_missing":
        session.delete(owner)
    elif guard == "owner_connection":
        owner.source_connection_id = owner.source_channel = None
    elif guard == "source_route":
        source.frozen_route = {
            **source.frozen_route,
            "authorization_revision": 999,
        }
    _, _, transport, apply = archive_context(session, source, child)
    assert not apply(child).deleted
    assert not transport.objects


def test_legacy_child_preserves_exact_context_ceiling_before_page_cleanup(
    session, directory_seed, monkeypatch
):
    from app.modules.sync_control.archive_children import register_legacy_owner_ceiling

    source, child = published_chain(session, directory_seed, monkeypatch)
    original = child.material_owner_observed_through
    refresh(session, directory_seed)
    child.material_owner_observed_through = None
    connection = session.connection()
    session.commit()

    def factory():
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    assert (
        register_legacy_owner_ceiling(
            factory,
            tenant_id=child.tenant_id,
            run_id=child.id,
            now=datetime.now(UTC) + timedelta(days=91),
        )
        == "material_child_owner_baseline_registered"
    )
    with factory() as db:
        assert (
            db.get(AdDirectoryRun, child.id).material_owner_observed_through == original
        )
        assert (
            db.exec(
                select(AdDirectoryPage).where(AdDirectoryPage.run_id == child.id)
            ).first()
            is not None
        )


def test_owner_change_during_child_upload_retains_original_and_no_committed_marker(
    session, directory_seed, monkeypatch
):
    source, child = published_chain(session, directory_seed, monkeypatch)
    refresh(session, directory_seed)
    _, factory, transport, apply = archive_context(session, source, child)

    def change_owner():
        with factory() as db:
            owner = db.get(
                AdObject, (source.tenant_id, source.advertiser_id, "ad", "ad-a")
            )
            owner.source_connection_id = owner.source_channel = None
            db.add(owner)
            db.commit()

    transport.after_put = change_owner
    result = apply(child)
    assert not result.deleted
    assert result.reason == "material_child_owner_source_mismatch"
    assert transport.objects and not any(
        "/committed/" in key for key in transport.objects
    )
    with factory() as db:
        assert db.get(AdDirectoryRun, child.id) is not None
        assert db.get(AdDirectoryRun, source.id) is not None


def test_missing_legacy_owner_cannot_register_guessed_baseline(
    session, directory_seed, monkeypatch
):
    source, child = published_chain(session, directory_seed, monkeypatch)
    child.material_owner_observed_through = None
    owner = session.get(
        AdObject, (source.tenant_id, source.advertiser_id, "ad", "ad-a")
    )
    session.delete(owner)
    _, factory, transport, apply = archive_context(session, source, child)
    assert apply(child).reason == "material_child_owner_missing"
    assert not transport.objects
    with factory() as db:
        assert db.get(AdDirectoryRun, child.id).material_owner_observed_through is None
