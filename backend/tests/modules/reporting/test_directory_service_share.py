"""目录登记到期头，实时/身份按工作类公平，租户及物理额度共同约束。"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import quota_configuration
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import refresh_report_directory
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from tests.modules.reporting.test_directory_direct_delivery import directory
from tests.modules.reporting.test_directory_refresh import prepared

KEY = "directory_service_v1"


@pytest.fixture(autouse=True)
def isolated_quota(monkeypatch, redis_client):
    del redis_client
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"directory-share-{uuid4()}")


def dispatch(session, redis, now, limit=40):
    return dispatch_directories_due(session, redis, now=now, limit=limit)


def finish(session, redis, run, now):
    from app.modules.ads.directory_dispatch import directory_step_key
    from app.modules.sync_control.contracts import ExecutionFence
    from app.modules.sync_control.directory_demand import (
        directory_endpoint,
        directory_outcome,
    )
    from app.modules.sync_control.occupancy import release_dispatch
    from app.modules.sync_control.requirements import satisfy_requirements

    # 运输替身在正式派发后模拟完成，保留真实头租约/代数和结果围栏；仅释放queued占位。
    release_dispatch(
        redis,
        reservation_id=directory_step_key(run),
        app_scope=settings.TIKTOK_APP_ID,
        endpoint=directory_endpoint(run),
        rate_group=directory_endpoint(run),
        tenant_id=run.tenant_id,
        advertiser_id=run.advertiser_id,
    )
    run.status = "COMPLETE"
    satisfy_requirements(
        session,
        fence=ExecutionFence(
            run.id, "directory", run.claim_generation, run.owner_epoch
        ),
        observed_at=now,
        publication_version=1,
    )
    directory_outcome(session, run)
    session.flush()


def pending(session, seed, service, now, *, old=False, child=False):
    run = directory(session, seed)
    run.created_at = now - timedelta(hours=5 if old else 1)
    run.query = run.query | ({KEY: service} if service else {})
    if child:
        run.query = run.query | {"material_child_v1": {}}
    run.next_attempt_at = now
    register_directory_run(session, run, new_execution=True)
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
def test_continuous_old_identity_flow_keeps_weighted_status_progress(
    session, sync_seed, redis_client, legacy
):
    now = datetime.now(UTC)
    for _ in range(48):
        pending(session, sync_seed, None if legacy else "identity", now, old=True)
    statuses = {pending(session, sync_seed, "status", now).id for _ in range(32)}
    classes = Counter()
    for turn in range(13):
        at = now + timedelta(seconds=turn)
        assert dispatch(session, redis_client, at, limit=1) == 1
        run = next(r for r in sent(session) if r.status != "COMPLETE")
        classes["realtime" if run.id in statuses else "directory"] += 1
        finish(session, redis_client, run, at)
    assert classes == {"realtime": 10, "directory": 3}


@pytest.mark.parametrize("occupied", ["status", "identity"])
def test_leased_head_does_not_prevent_other_class_refill(
    session, sync_seed, redis_client, occupied
):
    now = datetime.now(UTC)
    first = pending(session, sync_seed, occupied, now, old=True)
    assert dispatch(session, redis_client, now, limit=2) == 1
    assert [r.id for r in sent(session)] == [first.id]
    pending(session, sync_seed, occupied, now, old=True)
    desired = pending(
        session, sync_seed, "identity" if occupied == "status" else "status", now
    )
    assert dispatch(session, redis_client, now, limit=2) == 1
    assert {r.id for r in sent(session)} == {first.id, desired.id}
    assert dispatch(session, redis_client, now) == 0


@pytest.mark.parametrize("service", ["status", "identity", None])
def test_single_class_borrows_every_released_turn_without_duplicate_head_dispatch(
    session, sync_seed, redis_client, service
):
    now = datetime.now(UTC)
    for _ in range(8):
        pending(session, sync_seed, service, now)
    for turn in range(8):
        at = now + timedelta(seconds=turn)
        # 同一scope只有一个在途头，不再绕过账户准入一次派32个旧run。
        assert dispatch(session, redis_client, at) == 1
        assert dispatch(session, redis_client, at) == 0
        run = next(r for r in sent(session) if r.status != "COMPLETE")
        finish(session, redis_client, run, at)
    assert dispatch(session, redis_client, now + timedelta(seconds=9)) == 0


def test_initial_producer_only_wakes_dispatcher(session, sync_seed):
    now = datetime.now(UTC)
    pending(session, sync_seed, "status", now)
    newcomer = pending(session, sync_seed, "identity", now)
    directory_dispatch.enqueue_directory_step(session, newcomer, initial=True)
    assert sent(session) == []
    messages = session.exec(select(PendingDispatch)).all()
    assert len(messages) == 1 and messages[0].task_name == "reporting.plan_due"


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
    expected_service = "identity" if service == "identity" else "status_missing"
    assert all(r.query.get(KEY) == expected_service for r in runs)


@pytest.mark.parametrize(
    "guard", ["lease", "token", "page", "outbox", "route", "ids", "child"]
)
def test_hot_demand_does_not_move_inflight_or_unrelated_runs(
    session, sync_seed, monkeypatch, redis_client, guard
):
    from uuid import uuid4

    from app.modules.ads.sync import stage_directory_page
    from tests.modules.reporting.test_directory_direct_delivery import page

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    run = session.exec(select(AdDirectoryRun).where(AdDirectoryRun.kind == "ad")).one()
    run.query = run.query | {KEY: "identity"}
    if guard == "lease":
        run.claimed_until = datetime.now(UTC) + timedelta(minutes=1)
    elif guard == "token":
        run.claim_token = uuid4()
    elif guard == "page":
        stage_directory_page(
            session, run_id=run.id, page=page(complete=True), claim_generation=1
        )
    elif guard == "outbox":
        # 唤醒消息还不是执行投递；真实dispatcher领取后才禁止晋升。
        register_directory_run(session, run)
        assert (
            dispatch(session, redis_client, datetime.now(UTC) + timedelta(seconds=1))
            > 0
        )
        assert run.id in {r.id for r in sent(session)}
    elif guard == "route":
        run.frozen_route = run.frozen_route | {"binding_revision": 17}
    elif guard == "ids":
        run.query = run.query | {"ids": ["unrelated"]}
    else:
        run.query = run.query | {"material_child_v1": {}}
    session.flush()
    refresh_report_directory(session, job)
    assert run.query[KEY] == "identity"


def test_child_always_uses_directory_work_class_even_with_status_marker(
    session, sync_seed, redis_client
):
    from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement

    now = datetime.now(UTC)
    child = pending(session, sync_seed, "status", now, old=True, child=True)
    status = pending(session, sync_seed, "status", now)
    child_scope = session.exec(
        select(SyncExecutionScope).where(
            SyncExecutionScope.directory_run_id == child.id
        )
    ).one()
    assert (
        session.get(SyncRequirement, child_scope.requirement_id).source == "directory"
    )
    assert dispatch(session, redis_client, now, limit=1) == 1
    assert [r.id for r in sent(session)] == [status.id]


def test_work_classes_keep_tenant_rotation_and_continuous_progress(
    session, sync_seed, redis_client
):
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    other = seed_fixture.__wrapped__(session)
    now = datetime.now(UTC)
    for seed in (sync_seed, other):
        for _ in range(16):
            pending(session, seed, "identity", now, old=True)
            pending(session, seed, "status", now)
    turns = Counter()
    for turn in range(26):
        at = now + timedelta(seconds=turn)
        assert dispatch(session, redis_client, at, limit=1) == 1
        run = next(r for r in sent(session) if r.status != "COMPLETE")
        turns[(run.tenant_id, run.query[KEY])] += 1
        finish(session, redis_client, run, at)
    for seed in (sync_seed, other):
        assert turns[(seed.tenant_id, "status")] == 10
        assert turns[(seed.tenant_id, "identity")] == 3


def test_locked_runs_are_skipped_and_wire_payload_stays_unchanged(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        identities = [pending(db, seed, None, now, old=True).id for _ in range(4)]
        statuses = [pending(db, seed, "status", now).id for _ in range(4)]
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id.in_(statuses[:2]))
            .with_for_update()
        ).all()
        with factory() as db, db.begin():
            assert dispatch(db, redis_client, now, limit=2) == 2
            selected = {r.id for r in sent(db)}
            assert len(selected & set(statuses)) == 1
            assert not selected & set(statuses[:2])
            assert len(selected & set(identities)) == 1
            for message in db.exec(select(PendingDispatch)).all():
                assert set(message.payload) == {"run_id", "claim_generation"}


@pytest.mark.parametrize("unavailable", ["none", "future", "lease", "outbox"])
def test_initial_wake_then_dispatch_uses_ready_class_when_other_is_unavailable(
    session, sync_seed, redis_client, unavailable
):
    now = datetime.now(UTC)
    if unavailable != "none":
        other = pending(session, sync_seed, "status", now)
        if unavailable == "future":
            other.next_attempt_at = now + timedelta(hours=1)
        else:
            assert dispatch(session, redis_client, now, limit=2) == 1
            if unavailable == "lease":
                from app.modules.sync_control.directory_demand import directory_fence

                directory_fence(session, other, claim=True)
    newcomer = pending(session, sync_seed, "identity", now)
    directory_dispatch.enqueue_directory_step(session, newcomer, initial=True)
    assert newcomer.id not in {r.id for r in sent(session)}
    assert dispatch(session, redis_client, now, limit=2) == 1
    assert newcomer.id in {r.id for r in sent(session)}


@pytest.mark.parametrize("change", ["locked", "generation", "route"])
def test_promotion_rechecks_generation_scope_and_skips_locked_rows(
    committed_sync_seed, change
):
    from app.modules.ads.directory_services import promote_status_requests

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = pending(db, seed, None, datetime.now(UTC)).id
    with factory() as observer, observer.begin():
        old = observer.get(AdDirectoryRun, identity)
        with factory() as writer, writer.begin():
            actual = writer.exec(
                select(AdDirectoryRun)
                .where(AdDirectoryRun.id == identity)
                .with_for_update()
            ).one()
            if change == "generation":
                actual.claim_generation += 1
            elif change == "route":
                actual.frozen_route = actual.frozen_route | {"binding_revision": 8}
            if change == "locked":
                assert promote_status_requests(observer, [old]) == 0
        if change != "locked":
            assert promote_status_requests(observer, [old]) == 0


@pytest.mark.parametrize("service", ["status", "identity"])
def test_service_marker_does_not_change_query_partition(service):
    from app.modules.ads.directory_requests import _partition_key

    query = {"ids": ["one"], "include_deleted": True}
    assert _partition_key(query) == _partition_key(query | {KEY: service})


@pytest.mark.parametrize("service", ["status", "identity", None])
def test_serialized_old_or_marked_task_reads_without_private_metadata(
    committed_sync_seed, monkeypatch, redis_client, service
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.core.db import engine
    from app.jobs import outbox
    from app.modules.ads import tasks
    from tests.modules.reporting.test_directory_direct_delivery import page

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        now = datetime.now(UTC)
        identity = pending(db, seed, service, now).id
        assert dispatch(db, redis_client, now, limit=1) == 1
        generation = db.get(AdDirectoryRun, identity).claim_generation
    received = []

    @contextmanager
    def gateway(**_kwargs):
        def read(query):
            received.append(query)
            assert not hasattr(query, KEY)
            return page(complete=False)

        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *args, **kwargs: None)
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=generation,
        )
        == "CONTINUE"
    )
    assert len(received) == 1 and received[0].page == 1
    with factory() as db:
        run = db.get(AdDirectoryRun, identity)
        assert run.next_page == 2 and run.query.get(KEY) == service
