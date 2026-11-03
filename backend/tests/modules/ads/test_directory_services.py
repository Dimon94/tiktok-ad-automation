from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.directory_services import (
    SERVICE_KEY,
    directory_service,
    promote_status_requests,
)
from app.modules.ads.material_directory_children import material_child_scope
from app.modules.ads.models import AdObject
from app.modules.ads.sync import (
    _queue_missing_parents,
    publish_directory,
    stage_directory_page,
)
from app.modules.ads.sync_models import AdDirectoryRun
from tests.modules.ads.test_directory_sync import _authority, _run
from tests.modules.ads.test_material_directory_children import _source_page


@pytest.mark.parametrize("service", [None, "status", "identity"])
def test_missing_parent_inherits_service_without_guessing_legacy(
    session, directory_seed, monkeypatch, service
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    source.workload = "live"
    if service:
        source.query = source.query | {SERVICE_KEY: service}
    obj = AdObject(
        tenant_id=source.tenant_id,
        advertiser_id=source.advertiser_id,
        kind="ad",
        remote_id="ad",
        ad_type="SMART_PLUS",
        parent_kind="adgroup",
        parent_remote_id="missing-group",
    )
    _queue_missing_parents(session, run=source, objects=[obj])
    parent = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.kind == "adgroup")
    ).one()
    assert directory_service(parent) == service
    assert parent.frozen_route == source.frozen_route
    assert parent.workload == source.workload


def test_status_parent_demand_promotes_existing_identity_without_duplicate(
    session, directory_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    source.workload = "live"
    source.query = source.query | {SERVICE_KEY: "status"}
    parent = _run(session, directory_seed, kind="adgroup", ad_type="SMART_PLUS")
    parent.workload = "live"
    parent.query = parent.query | {"ids": ["group"], SERVICE_KEY: "identity"}
    session.flush()
    identity = (parent.id, parent.claim_generation, parent.partition_key)
    obj = AdObject(
        tenant_id=source.tenant_id,
        advertiser_id=source.advertiser_id,
        kind="ad",
        remote_id="ad",
        ad_type="SMART_PLUS",
        parent_kind="adgroup",
        parent_remote_id="group",
    )
    _queue_missing_parents(session, run=source, objects=[obj])
    parents = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.kind == "adgroup")
    ).all()
    assert len(parents) == 1
    assert (parent.id, parent.claim_generation, parent.partition_key) == identity
    assert directory_service(parent) == "identity"


@pytest.mark.parametrize("missing", [False, True])
def test_status_promotion_preserves_explicit_material_identity_demand(
    session, directory_seed, monkeypatch, missing
):
    from app.modules.sync_control.directory_demand import register_directory_run
    from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    source.workload = "live"
    source.query = source.query | {"ids": ["ad"], SERVICE_KEY: "identity"}
    register_directory_run(session, source, new_execution=True)
    assert (
        promote_status_requests(
            session, [source], missing_run_ids=[source.id] if missing else []
        )
        == 1
    )
    assert directory_service(source) == "identity"
    child = session.exec(
        select(SyncExecutionScope).where(
            SyncExecutionScope.directory_run_id == source.id
        )
    ).one()
    assert session.get(SyncRequirement, child.requirement_id).source == "realtime"
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(directory_seed, 1, [("ad", "group")], terminal=True),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    material = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.kind == "creative")
    ).one()
    assert directory_service(material) == "identity"


@pytest.mark.parametrize("service", [None, "status", "identity"])
def test_material_child_keeps_identity_scope_and_source_age(
    session, directory_seed, monkeypatch, service
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    source.workload = "live"
    source.created_at = datetime.now(UTC) - timedelta(hours=4)
    source.query = source.query | {"ids": ["ad"]}
    if service:
        source.query = source.query | {SERVICE_KEY: service}
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=_source_page(directory_seed, 1, [("ad", "group")], terminal=True),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    if service == "status":
        assert not session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.kind == "creative")
        ).all()
        return
    child = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.kind == "creative")
    ).one()
    assert directory_service(child) == "identity"
    assert child.query[SERVICE_KEY] == "identity"
    assert child.created_at == source.created_at and child.workload == source.workload
    assert child.frozen_route == source.frozen_route
    assert material_child_scope(session, child) is not None
