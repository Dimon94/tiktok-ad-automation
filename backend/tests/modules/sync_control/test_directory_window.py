"""目录读取/发布窗口分别按有效租约计数，保留跨扫描、owner与代数围栏。"""

from datetime import UTC, datetime, timedelta

import pytest
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


def test_full_publisher_window_does_not_block_remote_read_window(
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
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    # 两个窗口分别有界：本地发布排队不能挡读取，也不能无限追加读取。
    _directory(session, sync_seed, advertiser="acc-2", kind="campaign")
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


def test_full_read_window_does_not_block_publish_and_both_remain_bounded(
    session, sync_seed, redis_client
):
    from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync import stage_directory_page

    _directory(session, sync_seed)
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    run = _directory(session, sync_seed, advertiser="acc-2")
    stage_directory_page(
        session,
        run_id=run.id,
        page=DirectoryPage((), (), None, True, CallEvidence()),
        claim_generation=1,
    )
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    message = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "ads.publish_step")
    ).one()
    assert message.payload["run_id"] == str(run.id)
    waiting = _directory(session, sync_seed, advertiser="acc-2", kind="campaign")
    stage_directory_page(
        session,
        run_id=waiting.id,
        page=DirectoryPage((), (), None, True, CallEvidence()),
        claim_generation=1,
    )
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 0
    assert waiting.claim_generation == 1


def test_phase_discovery_cursors_do_not_hide_other_stage(
    session, sync_seed, redis_client
):
    from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
    from app.modules.ads.sync import stage_directory_page
    from app.modules.sync_control.directory_candidates import _candidate

    run = _directory(session, sync_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=DirectoryPage((), (), None, True, CallEvidence()),
        claim_generation=1,
    )
    args = {
        "tenant_id": sync_seed.tenant_id,
        "work_class": "directory",
        "now": datetime.now(UTC) + timedelta(seconds=1),
        "independent_only": False,
        "discover_only": True,
        "redis": redis_client,
    }
    assert _candidate(session, phase="read", **args) is None
    assert _candidate(session, phase="publish", **args)[1].id == run.id
    assert _candidate(session, phase="all", **args)[1].id == run.id
    # 短锁复验也拒绝已经不属于读取阶段的候选，不能只在发现SQL过滤。
    assert (
        _candidate(
            session,
            phase="read",
            selected=(
                session.exec(select(SyncSchedule.scope_id)).one(),
                run.id,
            ),
            **{**args, "discover_only": False},
        )
        is None
    )


@pytest.mark.parametrize("publisher_count", [39, 40])
def test_read_to_publish_handoff_obeys_publisher_window(
    session, sync_seed, redis_client, publisher_count
):
    from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
    from app.jobs.models import PendingDispatch
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.ads.sync import stage_directory_page
    from app.modules.sync_control.directory_demand import (
        directory_outcome,
        handoff_directory_publication,
    )

    publishers = []
    for index in range(publisher_count):
        advertiser = f"publisher-{index}"
        session.add(
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=advertiser,
                timezone="UTC",
            )
        )
        session.flush()
        run = _directory(session, sync_seed, advertiser=advertiser)
        stage_directory_page(
            session,
            run_id=run.id,
            page=DirectoryPage((), (), None, True, CallEvidence()),
            claim_generation=run.claim_generation,
        )
        publishers.append(run)
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert (
        dispatch_directories_due(session, redis_client, now=now, limit=40)
        == publisher_count
    )
    reading = _directory(session, sync_seed)
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == 1
    reading_generation = reading.claim_generation
    stage_directory_page(
        session,
        run_id=reading.id,
        page=DirectoryPage((), (), None, True, CallEvidence()),
        claim_generation=reading_generation,
    )
    reading.next_page = 2
    if publisher_count == 39:
        assert handoff_directory_publication(session, reading, now=now) is True
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.task_name == "ads.publish_step"
                    )
                ).all()
            )
            == 40
        )
        return
    # 已领取的读取完成末页也必须受同一个发布上限约束，不能直接挤进第41个。
    assert handoff_directory_publication(session, reading, now=now) is False
    directory_outcome(session, reading)
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "ads.publish_step"
                )
            ).all()
        )
        == 40
    )
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == 0
    # 释放一个真实发布位置后，原暂存末页由规范dispatcher换代恢复，无需重读HTTP。
    publishers[0].status = "COMPLETE"
    directory_outcome(session, publishers[0])
    assert dispatch_directories_due(session, redis_client, now=now, limit=40) == 1
    assert reading.claim_generation == reading_generation + 1
    messages = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "ads.publish_step",
            PendingDispatch.payload["run_id"].astext == str(reading.id),
        )
    ).all()
    assert len(messages) == 1
