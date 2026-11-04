"""到期头存在过期就绪信息时，不能让不可执行前缀挡住空闲接口和Worker。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.reporting import job_tasks  # noqa: F401
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.models import SyncSchedule
from tests.modules.sync_control.test_dispatch_interleavings import enqueue


@pytest.mark.parametrize("allow_backfill", [True, False])
def test_waiting_prefix_cannot_hide_a_ready_report(
    session, sync_seed, redis_client, allow_backfill
):
    now = datetime.now(UTC)
    blocked = []
    for index in range(25):
        identity = enqueue(
            session,
            sync_seed,
            now,
            tier="hot",
            accounts=["acc-1"],
            campaign=f"blocked-{index}",
        )
        job = session.get(ReportSyncJob, identity)
        job.status, job.due_at = "WAITING", now + timedelta(hours=1)
        blocked.append(identity)
    for head in session.exec(select(SyncSchedule)).all():
        head.last_dispatched_at = now - timedelta(days=1)
    ready = enqueue(
        session, sync_seed, now, tier="hot", accounts=["acc-2"], campaign="ready"
    )
    for head in session.exec(select(SyncSchedule)).all():
        if head.last_dispatched_at is None:
            head.last_dispatched_at = now - timedelta(hours=1)
    session.flush()
    assert (
        sum(
            dispatch_due(
                session,
                redis_client,
                now=now,
                physical_slots=1,
                allow_backfill=allow_backfill,
            )
            for _ in range(2)
        )
        == 1
    )
    assert session.get(ReportSyncJob, ready).status == "RUNNING"
    assert all(
        session.get(ReportSyncJob, identity).status == "WAITING" for identity in blocked
    )


@pytest.mark.parametrize("allow_backfill", [True, False])
def test_unready_tenant_cannot_keep_the_only_dispatch_opportunity(
    session, sync_seed, redis_client, allow_backfill
):
    from app.modules.reporting.store_models import ReportDispatchCursor
    from app.modules.sync_control.models import SyncCostLedger
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    other = seed_fixture.__wrapped__(session)
    now = datetime.now(UTC)
    for index in range(25):
        identity = enqueue(
            session,
            sync_seed,
            now,
            tier="hot",
            accounts=["acc-1"],
            campaign=f"blocked-{index}",
        )
        job = session.get(ReportSyncJob, identity)
        job.status, job.due_at = "WAITING", now + timedelta(hours=1)
    ready = enqueue(
        session, other, now, tier="hot", accounts=["acc-1"], campaign="ready"
    )
    session.add_all(
        [
            ReportDispatchCursor(
                tenant_id=sync_seed.tenant_id,
                last_dispatched_at=now - timedelta(days=2),
            ),
            ReportDispatchCursor(
                tenant_id=other.tenant_id,
                last_dispatched_at=now - timedelta(days=1),
            ),
        ]
    )
    session.flush()
    assert (
        sum(
            dispatch_due(
                session,
                redis_client,
                now=now,
                physical_slots=1,
                allow_backfill=allow_backfill,
            )
            for _ in range(2)
        )
        == 1
    )
    assert session.get(ReportSyncJob, ready).status == "RUNNING"
    assert not session.exec(
        select(SyncCostLedger).where(SyncCostLedger.tenant_id == sync_seed.tenant_id)
    ).all()


def test_future_worker_lease_is_part_of_actual_report_eligibility(session, sync_seed):
    from uuid import uuid4

    from app.modules.sync_control.schedule import pending_execution_dues

    now = datetime.now(UTC)
    identity = enqueue(
        session, sync_seed, now, tier="hot", accounts=["acc-1"], campaign="claimed"
    )
    job = session.get(ReportSyncJob, identity)
    job.claim_token, job.claimed_until = uuid4(), now + timedelta(minutes=4)
    session.flush()
    heads = session.exec(select(SyncSchedule)).all()
    dues = pending_execution_dues(
        session, heads, "report", now=now, respect_claims=True
    )
    assert dues[(heads[0].scope_id, heads[0].work_class)] == job.claimed_until
    assert (
        pending_execution_dues(session, heads, "report", now=now)[
            (heads[0].scope_id, heads[0].work_class)
        ]
        == job.due_at
    )


def test_report_outcome_does_not_keep_the_worker_lease_being_released(
    session, sync_seed, redis_client
):
    from app.modules.sync_control.schedule import record_report_outcome

    now = datetime.now(UTC)
    identity = enqueue(
        session, sync_seed, now, tier="hot", accounts=["acc-1"], campaign="continue"
    )
    assert dispatch_due(session, redis_client, now=now, physical_slots=1) == 1
    job = session.get(ReportSyncJob, identity)
    job.status, job.due_at = "WAITING", now + timedelta(seconds=1)
    # 正式调用方先发布outcome，再在同一事务清掉worker租约。
    record_report_outcome(session, job)
    job.claim_token = job.claimed_until = None
    session.flush()
    head = session.exec(select(SyncSchedule)).one()
    assert head.next_eligible_at == job.due_at
