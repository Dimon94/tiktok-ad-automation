"""统一需求合同：缺口范围不能被旧观察吞掉，scope与成本公平不依赖旧32槽。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.ads import directory_dispatch
from app.modules.ads.directory_services import directory_service
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
)
from tests.modules.reporting.directory_claiming import finish, pending, sent
from tests.modules.sync_control.conftest import (
    isolated_sync_quota as isolated_sync_quota,
)


def tick(session, redis, *, limit=1):
    return dispatch_directories_due(
        session, redis, now=datetime.now(UTC) + timedelta(seconds=1), limit=limit
    )


def test_completed_old_range_keeps_new_missing_requirement_and_reclaims(
    session, sync_seed, redis_client
):
    old = pending(session, sync_seed, age=60)
    missing = pending(session, sync_seed, "status_missing")
    assert session.exec(select(SyncSchedule)).one().active
    assert tick(session, redis_client) == 1
    assert [r.id for r in sent(session)] == [old.id]
    finish(session, redis_client, old, now=datetime.now(UTC))
    ref = session.exec(
        select(SyncExecutionScope).where(
            SyncExecutionScope.directory_run_id == missing.id
        )
    ).one()
    demand = session.get(SyncRequirement, ref.requirement_id)
    assert demand.satisfied_revision < demand.desired_revision
    assert session.exec(select(SyncSchedule)).one().active
    assert tick(session, redis_client) == 1
    assert {r.id for r in sent(session)} == {old.id, missing.id}
    finish(session, redis_client, missing, now=datetime.now(UTC))
    assert not session.exec(select(SyncSchedule)).one().active


def test_cost_classes_both_progress_with_single_available_slot(
    session, sync_seed, redis_client
):
    for i in range(16):
        pending(session, sync_seed, "identity", advertiser="acc-2", age=60)
        pending(session, sync_seed, "status_missing" if i % 2 else "status")
    classes = []
    for _ in range(13):
        assert tick(session, redis_client) == 1
        active = [r for r in sent(session) if r.status != "COMPLETE"]
        assert len(active) == 1
        classes.append(directory_service(active[0]))
        finish(session, redis_client, active[0], now=datetime.now(UTC))
    # 相同endpoint的实际单次发送成本50:15，13次应为10:3；不以调用次数伪装成本。
    assert classes.count("status") == 10 and classes.count("identity") == 3


def test_initial_producer_only_wakes_canonical_head(session, sync_seed, redis_client):
    old = pending(session, sync_seed, age=60)
    new = pending(session, sync_seed, "status_missing")
    directory_dispatch.enqueue_directory_step(session, new, initial=True)
    assert sent(session) == []
    assert tick(session, redis_client) == 1
    assert [r.id for r in sent(session)] == [old.id]


def test_missing_class_is_status_and_keeps_partition():
    from types import SimpleNamespace

    from app.modules.ads.directory_requests import _partition_key

    query = {"ids": ["one"], "include_deleted": False}
    marked = query | {"directory_service_v1": "status_missing"}
    assert directory_service(SimpleNamespace(query=marked)) == "status"
    assert _partition_key(marked) == _partition_key(query)


def test_page_continuation_wakes_but_cannot_bypass_fair_claim(
    session, sync_seed, redis_client
):
    run = pending(session, sync_seed, "status_missing")
    assert tick(session, redis_client) == 1
    first_generation = run.claim_generation
    run.next_page = 2
    directory_dispatch.enqueue_directory_step(session, run)
    assert len(sent(session)) == 1
    assert tick(session, redis_client) == 1
    assert run.claim_generation == first_generation + 1
    assert len(sent(session)) == 2


def test_rollback_preserves_requirements_and_fairness(session, sync_seed, redis_client):
    run = pending(session, sync_seed, "status_missing")
    before = run.claim_generation
    savepoint = session.begin_nested()
    assert tick(session, redis_client) == 1
    savepoint.rollback()
    session.expire_all()
    assert sent(session) == [] and run.claim_generation == before
    assert session.get(ReportDispatchCursor, sync_seed.tenant_id) is None
    assert session.exec(select(SyncSchedule)).one().lease_token is None
    # Redis短期预留不回滚，因此本断言不把再次发送当作数据库回滚成功的必要条件。
    ref = session.exec(select(SyncExecutionScope)).one()
    demand = session.get(SyncRequirement, ref.requirement_id)
    assert demand.satisfied_revision < demand.desired_revision


def test_cross_tenant_scope_rotation_is_not_swallowed_by_backlog(
    session, sync_seed, redis_client
):
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    other = seed_fixture.__wrapped__(session)
    for seed in (sync_seed, other):
        for i in range(8):
            pending(session, seed, "status_missing", advertiser=f"scope-{i}")
    assert tick(session, redis_client, limit=4) == 4
    assert sum(r.tenant_id == sync_seed.tenant_id for r in sent(session)) == 2
    assert sum(r.tenant_id == other.tenant_id for r in sent(session)) == 2
    assert tick(session, redis_client, limit=4) == 0


def test_single_work_class_uses_entire_execution_window(
    session, sync_seed, redis_client
):
    for i in range(6):
        pending(session, sync_seed, "status_missing", advertiser=f"scope-{i}")
    assert tick(session, redis_client, limit=4) == 4
    assert tick(session, redis_client, limit=4) == 0
    assert len(sent(session)) == 4


def test_locked_head_does_not_block_other_scope(committed_sync_seed, redis_client):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        first = pending(db, seed, "status_missing").id
        second = pending(db, seed, advertiser="acc-2").id
    with factory() as lock, lock.begin():
        lock.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == first).with_for_update()
        ).one()
        with factory() as db, db.begin():
            assert tick(db, redis_client) == 1
            assert [r.id for r in sent(db)] == [second]


def test_locked_other_scope_does_not_make_producer_send_directly(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        first = pending(db, seed, "status_missing").id
        second = pending(db, seed, advertiser="acc-2").id
    with factory() as lock, lock.begin():
        lock.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == first).with_for_update()
        ).one()
        with factory() as db, db.begin():
            directory_dispatch.enqueue_directory_step(
                db, db.get(AdDirectoryRun, second), initial=True
            )
            assert sent(db) == []
            assert tick(db, redis_client) == 1
            assert [r.id for r in sent(db)] == [second]


def test_missing_service_serialized_task_uses_existing_metadata_strip(
    committed_sync_seed, monkeypatch, redis_client
):
    from tests.modules.reporting.test_directory_service_share import (
        test_serialized_old_or_marked_task_reads_without_private_metadata,
    )

    test_serialized_old_or_marked_task_reads_without_private_metadata(
        committed_sync_seed, monkeypatch, redis_client, "status_missing"
    )


@pytest.mark.parametrize("terminal", [False, True])
def test_recovery_claim_preserves_page_and_uses_correct_delivery(
    session, sync_seed, redis_client, terminal
):
    from app.jobs.models import PendingDispatch

    run = pending(session, sync_seed, "status_missing")
    run.next_page = 1 if terminal else 2
    if terminal:
        session.add(
            AdDirectoryPage(
                run_id=run.id,
                page=1,
                tenant_id=run.tenant_id,
                advertiser_id=run.advertiser_id,
                claim_generation=run.claim_generation,
                complete=True,
                items=[],
                materials=[],
                evidence={},
            )
        )
    session.flush()
    assert tick(session, redis_client) == 1
    delivery = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name.in_(["ads.targeted_step", "ads.publish_step"])
        )
    ).one()
    assert delivery.task_name == (
        "ads.publish_step" if terminal else "ads.targeted_step"
    )
    assert run.next_page == (1 if terminal else 2)
    assert tick(session, redis_client) == 0
