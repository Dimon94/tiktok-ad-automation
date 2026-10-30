"""目录窗口按有效执行租约计数，跨扫描和发布续步保持同一上限。"""

from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncSchedule, SyncScope
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_same_execution_leased_by_two_heads_occupies_one_slot(
    session, sync_seed, redis_client
):
    first = _directory(session, sync_seed)
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=2) == 1
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.lease_execution_id == first.id)
    ).one()
    values = head.model_dump()
    values["work_class"] = "realtime"
    session.add(SyncSchedule(**values))
    _directory(session, sync_seed, advertiser="acc-2")
    assert dispatch_directories_due(session, redis_client, now=now, limit=2) == 1
    assert dispatch_directories_due(session, redis_client, now=now, limit=2) == 0


def test_expired_head_lease_releases_slot_and_reclaims_new_generation(
    session, sync_seed, redis_client
):
    run = _directory(session, sync_seed)
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
    ).one()
    generation = run.claim_generation
    head.leased_until = now - timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    assert run.claim_generation == generation + 1
    assert head.lease_execution_generation == run.claim_generation
    from app.modules.sync_control.models import SyncExecutionScope

    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.directory_run_id == run.id)
    ).one()
    assert child.execution_generation == run.claim_generation
    assert child.observed_at is None


def test_previous_owner_lease_cannot_consume_current_window(
    session, sync_seed, redis_client
):
    run = _directory(session, sync_seed)
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
    ).one()
    session.get(SyncScope, head.scope_id).owner_epoch += 1
    _directory(session, sync_seed, advertiser="acc-2")
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1


def test_terminal_page_waiting_for_publish_still_occupies_directory_window(
    session, sync_seed, redis_client
):
    from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync import stage_directory_page

    run = _directory(session, sync_seed)
    run.workload = "background"
    stage_directory_page(
        session,
        run_id=run.id,
        page=DirectoryPage((), (), None, True, CallEvidence()),
        claim_generation=1,
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    message = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "ads.publish_step")
    ).one()
    assert message.payload["run_id"] == str(run.id)
    _directory(session, sync_seed, advertiser="acc-2")
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 0


def test_live_worker_lease_keeps_slot_after_queue_head_timeout(
    session, sync_seed, redis_client
):
    run = _directory(session, sync_seed)
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
    ).one()
    # 模拟Worker租约仍有效而队列头先到期；头超时不代表远端调用已经停止。
    from app.modules.sync_control.directory_demand import directory_fence

    directory_fence(session, run, claim=True)
    run.status = "RUNNING"
    run.claimed_until = now + timedelta(minutes=1)
    head.leased_until = now - timedelta(seconds=1)
    _directory(session, sync_seed, advertiser="acc-2")
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 0
