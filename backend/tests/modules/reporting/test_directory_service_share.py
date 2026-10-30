"""实时状态与身份补录共用32槽，持续旧身份流不能饿死当前状态。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.directory_refresh import refresh_report_directory
from tests.modules.reporting.test_directory_direct_delivery import directory
from tests.modules.reporting.test_directory_refresh import prepared

KEY = "directory_service_v1"


def pending(session, seed, service, now, *, old=False, child=False):
    run = directory(session, seed)
    run.created_at = now - timedelta(hours=5 if old else 1)
    run.query = run.query | ({KEY: service} if service else {})
    if child:
        run.query = run.query | {"material_child_v1": {}}
    return run


def sent(session):
    return [
        session.get(AdDirectoryRun, UUID(message.payload["run_id"]))
        for message in session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "ads.targeted_step"
            )
        ).all()
    ]


@pytest.mark.parametrize("legacy", [False, True])
def test_continuous_old_identity_flow_reserves_half_for_status(
    session, sync_seed, legacy
):
    now = datetime.now(UTC)
    for _ in range(48):
        pending(session, sync_seed, None if legacy else "identity", now, old=True)
    statuses = [pending(session, sync_seed, "status", now) for _ in range(32)]
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    assert len({r.id for r in sent(session)} & {r.id for r in statuses}) == 16
    assert job_tasks._dispatch_directories(session, now) == 0


@pytest.mark.parametrize("occupied", ["status", "identity"])
def test_single_refill_honors_share_with_claims_and_batch_one(
    session, sync_seed, monkeypatch, occupied
):
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", 1)
    for _ in range(31):
        run = pending(session, sync_seed, occupied, now, old=True)
        run.status, run.claimed_until = "RUNNING", now + timedelta(minutes=1)
    pending(session, sync_seed, occupied, now, old=True)
    desired = pending(
        session, sync_seed, "identity" if occupied == "status" else "status", now
    )
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 1
    assert [r.id for r in sent(session)] == [desired.id]


@pytest.mark.parametrize("service", ["status", "identity", None])
def test_unused_live_share_is_fully_borrowed(session, sync_seed, service):
    now = datetime.now(UTC)
    for _ in range(33):
        pending(session, sync_seed, service, now)
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    assert job_tasks._dispatch_directories(session, now) == 0


def test_initial_producer_does_not_take_other_services_reserved_slot(
    session, sync_seed
):
    now = datetime.now(UTC)
    for _ in range(16):
        run = pending(session, sync_seed, "identity", now)
        run.status, run.claimed_until = "RUNNING", now + timedelta(minutes=1)
    pending(session, sync_seed, "status", now)
    newcomer = pending(session, sync_seed, "identity", now)
    session.flush()
    directory_dispatch.enqueue_directory_step(session, newcomer, initial=True)
    assert sent(session) == []


@pytest.mark.parametrize("service", [None, "identity"])
def test_hot_fact_reuses_and_promotes_unstarted_old_run(
    session, sync_seed, monkeypatch, service
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    runs = session.exec(select(AdDirectoryRun)).all()
    for run in runs:
        run.query = {k: v for k, v in run.query.items() if k != KEY}
        if service:
            run.query = run.query | {KEY: service}
    session.flush()
    identities = [
        (r.id, r.partition_key, r.claim_generation, r.created_at) for r in runs
    ]
    assert refresh_report_directory(session, job) == 0
    assert [
        (r.id, r.partition_key, r.claim_generation, r.created_at) for r in runs
    ] == identities
    assert all(r.query.get(KEY) == "status" for r in runs)


@pytest.mark.parametrize("batch", [1, 7, 10])
def test_small_batches_keep_shares_across_refills(
    session, sync_seed, monkeypatch, batch
):
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", batch)
    now = datetime.now(UTC)
    for _ in range(40):
        pending(session, sync_seed, "identity", now, old=True)
        pending(session, sync_seed, "status", now)
    session.flush()
    while job_tasks._dispatch_directories(session, now):
        pass
    runs = sent(session)
    assert len(runs) == 32
    assert sum(r.query.get(KEY) == "status" for r in runs) == 16


def test_material_children_do_not_use_status_reservation(session, sync_seed):
    now = datetime.now(UTC)
    for _ in range(40):
        pending(session, sync_seed, "status", now, child=True, old=True)
    statuses = {pending(session, sync_seed, "status", now).id for _ in range(20)}
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    assert len({r.id for r in sent(session)} & statuses) == 16


@pytest.mark.parametrize(
    "blocked",
    ["message", "old_message", "page", "claimed", "child", "published", "background"],
)
def test_promotion_keeps_started_or_special_runs_unchanged(session, sync_seed, blocked):
    from app.modules.ads.directory_services import promote_status_requests
    from app.modules.ads.sync_models import AdDirectoryPage

    run = pending(session, sync_seed, "identity", datetime.now(UTC))
    if blocked in ("message", "old_message"):
        directory_dispatch.enqueue_directory_step(session, run)
        if blocked == "old_message":
            run.claim_generation += 1
    elif blocked == "page":
        session.add(
            AdDirectoryPage(
                run_id=run.id,
                tenant_id=run.tenant_id,
                advertiser_id=run.advertiser_id,
                claim_generation=1,
                page=1,
                items=[],
                materials=[],
                evidence={},
                complete=False,
            )
        )
    elif blocked == "claimed":
        run.claimed_until = datetime.now(UTC) + timedelta(minutes=1)
    elif blocked == "child":
        run.query = run.query | {"material_child_v1": {}}
    elif blocked == "published":
        run.published_version = 1
    else:
        run.workload = "background"
    session.flush()
    original = dict(run.query)
    assert promote_status_requests(session, [run]) == 0
    assert run.query == original


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "bc_id",
        "advertiser_id",
        "actor_id",
        "frozen_route",
        "claim_generation",
        "partition_key",
        "query",
    ],
)
def test_promotion_cas_preserves_changed_frozen_identity(session, sync_seed, field):
    from app.modules.ads.directory_services import promote_status_requests

    run = pending(session, sync_seed, "identity", datetime.now(UTC))
    session.flush()
    changed = {
        "tenant_id": sync_seed.actor_id,
        "bc_id": "another-bc",
        "advertiser_id": "acc-2",
        "partition_key": "other-partition",
        "actor_id": sync_seed.tenant_id,
        "frozen_route": run.frozen_route | {"bc_id": "another-bc"},
        "claim_generation": 2,
        "query": run.query | {"ids": ["another-ad"]},
    }[field]
    # 用过时的读取快照调用，不能覆盖数据库里已变化的冻结范围或代数。
    stale = run.model_copy(update={field: changed})
    assert promote_status_requests(session, [stale]) == 0
    session.refresh(run)
    assert getattr(run, field) != changed
    assert run.query.get(KEY) == "identity"


def test_service_is_local_metadata_not_a_new_query_partition(session, sync_seed):
    from app.modules.ads.directory_requests import _partition_key

    run = pending(session, sync_seed, "identity", datetime.now(UTC))
    query = {k: v for k, v in run.query.items() if k != KEY}
    assert _partition_key(query) == _partition_key(query | {KEY: "identity"})
    assert _partition_key(query) == _partition_key(query | {KEY: "status"})


def test_locked_status_heads_keep_both_live_reservations(committed_sync_seed):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        statuses = [pending(db, seed, "status", now).id for _ in range(20)]
        for _ in range(32):
            pending(db, seed, "identity", now, old=True)
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id.in_(statuses[:4]))
            .with_for_update()
        ).all()
        with factory() as db, db.begin():
            assert job_tasks._dispatch_directories(db, now) == 32
            delivered = sent(db)
            assert {r.id for r in delivered if r.query.get(KEY) == "status"} == set(
                statuses[4:]
            )


def test_promotion_skips_locked_candidate_without_blocking(committed_sync_seed):
    from app.modules.ads.directory_services import promote_status_requests

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = pending(db, seed, "identity", datetime.now(UTC)).id
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id == identity)
            .with_for_update()
        ).one()
        with factory() as db, db.begin():
            run = db.get(AdDirectoryRun, identity)
            assert promote_status_requests(db, [run]) == 0
            assert run.query.get(KEY) == "identity"
    with factory() as db, db.begin():
        run = db.get(AdDirectoryRun, identity)
        assert promote_status_requests(db, [run]) == 1
        assert run.query.get(KEY) == "status"


@pytest.mark.parametrize("service", [None, "status", "identity"])
def test_missing_parent_inherits_source_service(
    session, sync_seed, monkeypatch, service
):
    from app.modules.ads.models import AdObject
    from app.modules.ads.sync import _queue_missing_parents

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    source = pending(session, sync_seed, service, now)
    obj = AdObject(
        tenant_id=source.tenant_id,
        advertiser_id=source.advertiser_id,
        kind="ad",
        remote_id="child-ad",
        parent_kind="adgroup",
        parent_remote_id="missing-group",
        ad_type=source.ad_type,
        observed_at=now,
        published_version=1,
    )
    _queue_missing_parents(session, run=source, objects=[obj])
    parent = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.id != source.id)
    ).one()
    assert parent.query.get(KEY) == service
    assert parent.frozen_route == source.frozen_route


def test_material_link_dependency_is_explicitly_identity(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.directory_refresh import request_link_directory
    from tests.modules.reporting.test_bridge_dependencies import seed_bridge

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, _ = seed_bridge(session, sync_seed, complete=False)
    assert request_link_directory(session, job, now=datetime.now(UTC)) == 1
    run = session.exec(select(AdDirectoryRun)).one()
    assert run.query.get(KEY) == "identity"
    assert run.frozen_route == job.frozen_route
