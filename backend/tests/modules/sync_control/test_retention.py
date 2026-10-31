"""终态证明与幂等身份保留，不能用清理把等待任务变成已完成。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.context import TenantContext
from app.jobs.models import PendingDispatch
from app.jobs.outbox import compact_published_dispatches, enqueue_after_commit
from app.modules.reporting.store_models import ReportSyncJob


def seed_dispatch(session, seed, *, status="COMPLETE", published=True):
    from app.jobs.tasks import register_dispatch_task

    register_dispatch_task("reporting.job_step", "ads-reporting")
    now = datetime.now(UTC)
    job = ReportSyncJob(
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        priority=3,
        status=status,
        contract_version=1,
        advertiser_ids=["acc-1"],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
        job_key=str(uuid4()),
        frozen_route={},
        actor_id=seed.actor_id,
        due_at=now,
        finished_at=now - timedelta(days=8),
    )
    session.add(job)
    session.flush()
    context = TenantContext(
        tenant_id=seed.tenant_id, actor_id=seed.actor_id, role="operator"
    )
    payload = {"job_id": str(job.id), "claim_id": str(uuid4())}
    key = f"reporting:{job.id}:old-claim"
    identifier = enqueue_after_commit(
        session,
        context=context,
        task_name="reporting.job_step",
        task_key=key,
        payload=payload,
    )
    # 模拟离线接管前的legacy消息；新V2生产者必须走envelope入口。
    job.contract_version = 2
    row = session.get(PendingDispatch, identifier)
    row.published_at = now - timedelta(days=8) if published else None
    session.flush()
    return job, row, context, payload, key


def test_compact_terminal_sync_keeps_replay_identity(session, sync_seed):
    job, row, context, payload, key = seed_dispatch(session, sync_seed)
    assert compact_published_dispatches(session, limit=10) == 1
    assert row.payload == {} and row.payload_digest and row.compacted_at
    assert (
        enqueue_after_commit(
            session,
            context=context,
            task_name="reporting.job_step",
            task_key=key,
            payload=payload,
        )
        == row.id
    )
    assert job.status == "COMPLETE"


def test_active_unpublished_and_unknown_sync_dispatches_are_preserved(
    session, sync_seed
):
    _, active, *_ = seed_dispatch(session, sync_seed, status="WAITING")
    _, unpublished, *_ = seed_dispatch(session, sync_seed, published=False)
    _, invalid, *_ = seed_dispatch(session, sync_seed)
    invalid.payload = {"job_id": "not-a-uuid", "claim_id": str(uuid4())}
    session.flush()
    assert compact_published_dispatches(session, limit=10) == 0
    assert active.payload and unpublished.payload and invalid.payload


def test_terminal_with_unpublished_child_is_not_compacted(session, sync_seed):
    from app.modules.sync_control.report_demand import adopt_report_job

    job, row, *_ = seed_dispatch(session, sync_seed)
    job.frozen_route = {
        "tenant_id": str(sync_seed.tenant_id),
        "bc_id": "bc-a",
        "connection_id": str(sync_seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "adapter_contract_revision": "official-api-v1",
        "binding_revision": 0,
    }
    adopt_report_job(session, job)
    session.flush()
    assert compact_published_dispatches(session, limit=10) == 0
    assert row.payload


def test_bounded_compaction_advances_past_protected_prefix(session, sync_seed):
    from app.modules.sync_control.retention import compact_sync_dispatches

    protected = [
        seed_dispatch(session, sync_seed, status="WAITING")[1] for _ in range(5)
    ]
    _, eligible, *_ = seed_dispatch(session, sync_seed)
    cursors = {}
    cutoff = datetime.now(UTC) - timedelta(days=7)
    for _ in range(6):
        compact_sync_dispatches(session, limit=2, cutoff=cutoff, cursors=cursors)
    assert eligible.compacted_at is not None
    assert all(row.payload and row.compacted_at is None for row in protected)
    # 一轮扫完后会重新访问曾受保护的记录，不永久跳过后来终态的任务。
    job = session.get(ReportSyncJob, protected[0].payload["job_id"])
    job.status = "COMPLETE"
    for _ in range(6):
        compact_sync_dispatches(session, limit=2, cutoff=cutoff, cursors=cursors)
    assert protected[0].compacted_at is not None


def test_old_pure_wake_is_compacted_but_business_planning_signal_is_preserved(
    session, sync_seed
):
    from app.jobs.tasks import register_dispatch_task

    register_dispatch_task("reporting.plan_due", "control")
    context = TenantContext(
        tenant_id=sync_seed.tenant_id, actor_id=sync_seed.actor_id, role="operator"
    )
    rows = []
    for key, payload in [
        ("sync-directory-wake:example:123", {"dispatch_only": True}),
        ("ordinary-plan:123", {"dispatch_only": True}),
        ("sync-directory-wake:example:124", {"dispatch_only": False}),
    ]:
        identifier = enqueue_after_commit(
            session,
            context=context,
            task_name="reporting.plan_due",
            task_key=key,
            payload=payload,
        )
        row = session.get(PendingDispatch, identifier)
        row.published_at = datetime.now(UTC) - timedelta(days=8)
        rows.append(row)
    session.flush()
    assert compact_published_dispatches(session, limit=10) == 1
    assert rows[0].payload == {} and rows[0].payload_digest
    assert rows[1].payload and rows[2].payload


def test_superseded_dispatch_compacts_only_after_terminal_proof(session, sync_seed):
    job, row, context, payload, key = seed_dispatch(
        session, sync_seed, published=False, status="WAITING"
    )
    row.superseded_at = datetime.now(UTC) - timedelta(days=8)
    session.flush()
    assert compact_published_dispatches(session, limit=10) == 0
    assert row.payload == payload and row.published_at is None
    job.status = "COMPLETE"
    session.flush()
    assert compact_published_dispatches(session, limit=10) == 1
    assert row.payload == {} and row.published_at is None and row.compacted_at
    assert (
        enqueue_after_commit(
            session,
            context=context,
            task_name="reporting.job_step",
            task_key=key,
            payload=payload,
        )
        == row.id
    )


@pytest.mark.parametrize("final_state", ["complete", "abandoned"])
def test_unsettled_or_unknown_cost_prevents_parent_cleanup(
    session, sync_seed, final_state
):
    from sqlmodel import select

    from app.modules.reporting.job_tasks import cleanup_store
    from app.modules.sync_control.models import SyncCostLedger

    job, row, *_ = seed_dispatch(session, sync_seed)
    assert compact_published_dispatches(session, limit=10) == 1
    receipt = SyncCostLedger(
        tenant_id=sync_seed.tenant_id,
        report_job_id=job.id,
        generation=1,
        physical_endpoint="test.report",
        work_class="history",
        eligible_weights={"history": 30},
        endpoint_weights={},
        reserved_units=5,
        observed_units=3,
        charged_units=5,
        receipt_version=2,
        settled_version=1,
        closed=True,
        lease_until=datetime.now(UTC) - timedelta(days=8),
    )
    session.add(receipt)
    session.flush()
    cleanup_store(session, now=datetime.now(UTC), categories=("jobs",))
    assert session.exec(
        select(ReportSyncJob.id).where(ReportSyncJob.id == job.id)
    ).one()
    receipt.settled_version = receipt.receipt_version
    receipt.closed = False  # 没有最终收据的死亡进程不能凭过期假定结算完成。
    session.flush()
    cleanup_store(session, now=datetime.now(UTC), categories=("jobs",))
    assert session.exec(
        select(ReportSyncJob.id).where(ReportSyncJob.id == job.id)
    ).one()
    if final_state == "complete":
        receipt.closed = True
    else:
        receipt.abandoned_at = datetime.now(UTC) - timedelta(days=7)
    session.flush()
    cleanup_store(session, now=datetime.now(UTC), categories=("jobs",))
    # 即使成本已结算，90天摘要和其证据仍须经持久归档后回收。
    assert session.exec(
        select(ReportSyncJob.id).where(ReportSyncJob.id == job.id)
    ).all()
    assert session.exec(
        select(SyncCostLedger.id).where(SyncCostLedger.id == receipt.id)
    ).all()
