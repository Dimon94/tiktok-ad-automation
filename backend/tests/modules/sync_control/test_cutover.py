"""停止旧消费者后的原地迁移，不删私有页、不丢需求、不接管有效租约。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.reporting.store_models import ReportSyncJob, ReportSyncPage
from app.modules.sync_control.models import SyncExecutionScope


def legacy_job(session, seed, *, status="QUEUED", live=False):
    now = datetime.now(UTC)
    route = {
        "tenant_id": str(seed.tenant_id),
        "bc_id": "bc-a",
        "connection_id": str(seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "adapter_contract_revision": "official-api-v1",
        "binding_revision": 0,
    }
    job = ReportSyncJob(
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        kind="ad_day",
        priority=3,
        status=status,
        advertiser_ids=["acc-1"],
        campaign_ids=[],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 2),
        job_key=f"legacy:{uuid4()}",
        frozen_route=route,
        due_at=now,
        contract_version=1,
        claim_token=uuid4() if status == "RUNNING" else None,
        claimed_until=(
            now + timedelta(minutes=5) if live else now - timedelta(minutes=1)
        )
        if status == "RUNNING"
        else None,
        progress={"marker": "preserve-exactly"},
    )
    session.add(job)
    session.flush()
    page = ReportSyncPage(
        job_id=job.id,
        page=1,
        tenant_id=seed.tenant_id,
        scope_key="frozen-page",
        rows=[{"dimensions": {"ad_id_v2": "a1"}, "metrics": {"spend": "1.25"}}],
        row_keys=["a1"],
        total_page=2,
        total_number=2,
    )
    session.add(page)
    session.flush()
    return job, page


def test_cutover_requires_explicit_stopped_consumers_and_preserves_rows(
    session, sync_seed
):
    from app.modules.sync_control.migration import adopt_legacy_batch

    job, page = legacy_job(session, sync_seed)
    with pytest.raises(ValueError, match="stopped"):
        adopt_legacy_batch(session, kind="report", consumers_stopped=False)
    assert (
        job.contract_version == 1
        and session.get(ReportSyncPage, (job.id, 1)).rows == page.rows
    )


def test_cutover_replay_preserves_checkpoint_and_maps_each_day_once(session, sync_seed):
    from app.modules.sync_control.migration import adopt_legacy_batch

    job, page = legacy_job(session, sync_seed, status="RUNNING")
    token = job.claim_token
    before = (page.rows.copy(), page.scope_key, job.progress.copy())
    result = adopt_legacy_batch(session, kind="report", consumers_stopped=True)
    assert result.adopted == 1 and result.deferred == 0 and not result.errors
    assert (
        job.id == page.job_id and job.claim_token is None and job.claim_token != token
    )
    assert job.status == "WAITING" and job.contract_version == 2
    assert (page.rows, page.scope_key, job.progress) == before
    children = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job.id)
    ).all()
    assert len(children) == 2 and all(c.status == "PENDING" for c in children)
    assert (
        adopt_legacy_batch(session, kind="report", consumers_stopped=True).adopted == 0
    )


def test_cutover_leaves_live_claim_untouched(session, sync_seed):
    from app.modules.sync_control.migration import adopt_legacy_batch

    job, page = legacy_job(session, sync_seed, status="RUNNING", live=True)
    token = job.claim_token
    result = adopt_legacy_batch(session, kind="report", consumers_stopped=True)
    assert result.adopted == 0 and result.deferred == 1
    assert (
        job.claim_token == token
        and job.status == "RUNNING"
        and job.contract_version == 1
    )
    assert session.get(ReportSyncPage, (job.id, 1)) is not None


def test_failed_adoption_rolls_back_only_bad_job_and_cursor_can_resume(
    session, sync_seed
):
    from app.modules.sync_control.migration import adopt_legacy_batch

    bad, bad_page = legacy_job(session, sync_seed, status="RUNNING")
    good, good_page = legacy_job(session, sync_seed)
    bad.advertiser_ids = ["missing-account"]
    original_token = bad.claim_token
    session.flush()
    result = adopt_legacy_batch(session, kind="report", consumers_stopped=True)
    assert result.adopted == 1 and len(result.errors) == 1
    assert result.errors[0]["id"] == str(bad.id)
    session.refresh(bad)
    assert bad.contract_version == 1 and bad.claim_token == original_token
    assert bad.status == "RUNNING" and bad_page.rows
    assert good.contract_version == 2 and good_page.rows
    bad.advertiser_ids = ["acc-1"]
    session.flush()
    replay = adopt_legacy_batch(session, kind="report", consumers_stopped=True)
    assert replay.adopted == 1 and not replay.errors


def test_bounded_cursor_visits_each_job_without_discarding_checkpoints(
    session, sync_seed
):
    from app.modules.sync_control.migration import adopt_legacy_batch

    pairs = [legacy_job(session, sync_seed) for _ in range(3)]
    cursor = None
    adopted = 0
    while True:
        result = adopt_legacy_batch(
            session, kind="report", consumers_stopped=True, after_id=cursor, limit=1
        )
        adopted += result.adopted
        if not result.scanned:
            break
        cursor = result.cursor
    assert adopted == 3
    assert all(
        session.get(ReportSyncPage, (job.id, 1)).rows == page.rows
        for job, page in pairs
    )


def test_directory_adoption_keeps_next_page_and_staged_body(session, sync_seed):
    from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
    from app.modules.sync_control.migration import adopt_legacy_batch

    job, _ = legacy_job(session, sync_seed)
    now = datetime.now(UTC)
    run = AdDirectoryRun(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        connection_id=sync_seed.connection.id,
        frozen_route=job.frozen_route,
        partition_key="b" * 64,
        query={},
        kind="ad",
        ad_type="REGULAR",
        status="RUNNING",
        claim_token=uuid4(),
        claimed_until=now - timedelta(seconds=1),
        claim_generation=1,
        contract_version=1,
        next_page=2,
        observed_at=now,
    )
    session.add(run)
    session.flush()
    page = AdDirectoryPage(
        run_id=run.id,
        page=1,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        claim_generation=1,
        complete=False,
        items=[{"id": "ad-old"}],
        materials=[],
        next_page=2,
    )
    session.add(page)
    session.flush()
    result = adopt_legacy_batch(session, kind="directory", consumers_stopped=True)
    assert result.adopted == 1 and not result.errors
    assert run.status == "QUEUED" and run.next_page == 2 and run.observed_at == now
    assert run.claim_generation == 2 and run.claim_token is None
    assert session.get(AdDirectoryPage, (run.id, 1)).items == [{"id": "ad-old"}]
    assert (
        adopt_legacy_batch(session, kind="directory", consumers_stopped=True).adopted
        == 0
    )


def test_balance_adoption_maps_only_remaining_accounts_and_keeps_page(
    session, sync_seed
):
    from app.modules.reporting.store_models import ReportBalanceRun
    from app.modules.sync_control.migration import adopt_legacy_batch

    job, _ = legacy_job(session, sync_seed)
    run = ReportBalanceRun(
        tenant_id=sync_seed.tenant_id,
        actor_id=sync_seed.actor_id,
        run_key=str(uuid4()),
        frozen_route=job.frozen_route,
        remaining_advertiser_ids=["acc-1"],
        next_page=3,
        contract_version=1,
    )
    session.add(run)
    session.flush()
    result = adopt_legacy_batch(session, kind="balance", consumers_stopped=True)
    assert result.adopted == 1 and not result.errors
    assert run.next_page == 3 and run.remaining_advertiser_ids == ["acc-1"]
    assert (
        len(
            session.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.balance_run_id == run.id
                )
            ).all()
        )
        == 1
    )
    assert (
        adopt_legacy_batch(session, kind="balance", consumers_stopped=True).adopted == 0
    )


def test_balance_cutover_retires_only_mapped_old_messages(session, sync_seed):
    from app.jobs.models import PendingDispatch
    from app.jobs.outbox import enqueue_after_commit
    from app.modules.reporting import balance_tasks  # noqa: F401 - 注册真实消费者
    from app.modules.reporting.store_models import ReportBalanceRun
    from app.modules.sync_control.migration import (
        adopt_legacy_batch,
        retire_legacy_balance_dispatches,
    )

    old_job, _ = legacy_job(session, sync_seed)
    run = ReportBalanceRun(
        tenant_id=sync_seed.tenant_id,
        actor_id=sync_seed.actor_id,
        run_key=f"cutover-outbox:{uuid4()}",
        frozen_route=old_job.frozen_route,
        remaining_advertiser_ids=["acc-1"],
        next_page=3,
        contract_version=1,
    )
    session.add(run)
    session.flush()
    payload = {"run_id": str(run.id), "page": 3}
    old_id = enqueue_after_commit(
        session,
        context=sync_seed.context,
        task_name="reporting.balance_step",
        task_key=f"old:{run.id}",
        payload=payload,
    )
    new_id = enqueue_after_commit(
        session,
        context=sync_seed.context,
        task_name="reporting.balance_step",
        task_key=f"new:{run.id}",
        payload={**payload, "claim_id": str(uuid4()), "claim_generation": 2},
    )
    retired_id = enqueue_after_commit(
        session,
        context=sync_seed.context,
        task_name="reporting.balance_step",
        task_key=f"retired:{run.id}",
        payload=payload,
    )
    # 已退役消息即使正文符合旧格式，也不能再次参与离线接管。
    retired = session.get(PendingDispatch, retired_id)
    retired.dispatch_contract_version = 2
    retired.issued_at = datetime.now(UTC) - timedelta(hours=1)
    retired.valid_until = datetime.now(UTC) + timedelta(hours=1)
    retired.retired_at = datetime.now(UTC)
    session.flush()
    original = session.get(PendingDispatch, old_id)
    digest = original.payload_digest
    with pytest.raises(ValueError, match="stopped"):
        retire_legacy_balance_dispatches(session, consumers_stopped=False)
    assert retire_legacy_balance_dispatches(session, consumers_stopped=True) == 0
    assert (
        adopt_legacy_batch(session, kind="balance", consumers_stopped=True).adopted == 1
    )
    assert (
        retire_legacy_balance_dispatches(session, consumers_stopped=True, limit=1) == 1
    )
    assert retire_legacy_balance_dispatches(session, consumers_stopped=True) == 0
    session.refresh(original)
    assert original.superseded_at is not None and original.published_at is None
    assert original.payload == payload and original.payload_digest == digest
    assert session.get(PendingDispatch, new_id).superseded_at is None
    assert session.get(PendingDispatch, retired_id).superseded_at is None
    assert run.next_page == 3 and run.remaining_advertiser_ids == ["acc-1"]


def test_pool_configuration_requires_shutdown_and_preserves_fairness(session):
    from app.modules.sync_control.migration import configure_background_pool
    from app.modules.sync_control.models import SyncWorkerPool

    with pytest.raises(ValueError, match="stopped"):
        configure_background_pool(session, consumers_stopped=False, capacity=4)
    with pytest.raises(ValueError):
        configure_background_pool(session, consumers_stopped=True, prefetch_slots=-1)
    row = configure_background_pool(
        session, consumers_stopped=True, capacity=4, prefetch_slots=4
    )
    row.fairness_state = {"history": 13.0}
    session.flush()
    again = configure_background_pool(session, consumers_stopped=True, capacity=8)
    assert again.capacity == 8 and again.prefetch_slots == 4
    assert again.fairness_state == {"history": 13.0}
    assert session.get(SyncWorkerPool, "background").capacity == 8
