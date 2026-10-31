"""请求成本与调度机会分开：真实累计、未知成本保守保留、代数独立结算。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.costs import (
    apply_cost_delta,
    record_cost_receipt,
    reserve_execution_cost,
    settle_costs,
)
from app.modules.sync_control.fairness import lock_tenant_cursor
from app.modules.sync_control.models import SyncCostLedger
from tests.modules.reporting.test_jobs import _route

ENDPOINT = "GET /open_api/v1.3/report/integrated/get/"
WEIGHTS = {"realtime": 50, "history": 30}


def reserve(session, seed, *, generation=1, units=50):
    if generation == 1:
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=date(2026, 10, 1),
            end=date(2026, 10, 1),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        job = (
            session.get(ReportSyncJob, job_id)
            if job_id
            else session.exec(
                select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
            ).one()
        )
    else:
        job = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).one()
    job.claim_generation = generation
    cursor = lock_tenant_cursor(session, seed.tenant_id)
    state = apply_cost_delta(
        cursor.fairness_state.get(ENDPOINT, {}), WEIGHTS, "history", 1
    )
    ledger = reserve_execution_cost(
        session,
        cursor=cursor,
        execution=job,
        kind="report",
        endpoint=ENDPOINT,
        work_class="history",
        eligible_weights=WEIGHTS,
        state_after_unit_charge=state,
        reserved_units=units,
        lease_until=datetime.now(UTC) + timedelta(minutes=4),
    )
    session.flush()
    return ledger, cursor


def test_physical_cost_receipts_settle_idempotently_and_do_not_refund_unknown(
    session, sync_seed
):
    ledger, cursor = reserve(session, sync_seed)
    producer = uuid4()
    record_cost_receipt(session, ledger.id, producer, total_units=7, closed=False)
    assert settle_costs(session, cursor) == 0
    assert ledger.charged_units == 50 and not ledger.closed
    record_cost_receipt(session, ledger.id, producer, total_units=7, closed=True)
    assert settle_costs(session, cursor) == 1
    expected = apply_cost_delta({}, WEIGHTS, "history", 7)
    assert cursor.fairness_state[ENDPOINT] == expected
    assert ledger.charged_units == ledger.observed_units == 7
    record_cost_receipt(session, ledger.id, producer, total_units=7, closed=True)
    assert settle_costs(session, cursor) == 0


def test_local_rejection_is_zero_cost_and_parallel_producers_do_not_hide_attempts(
    session, sync_seed
):
    ledger, cursor = reserve(session, sync_seed)
    first, duplicate = uuid4(), uuid4()
    record_cost_receipt(session, ledger.id, first, total_units=0, closed=True)
    settle_costs(session, cursor)
    assert ledger.charged_units == 0
    record_cost_receipt(session, ledger.id, duplicate, total_units=3, closed=False)
    record_cost_receipt(session, ledger.id, first, total_units=2, closed=True)
    record_cost_receipt(session, ledger.id, duplicate, total_units=3, closed=True)
    settle_costs(session, cursor)
    assert ledger.observed_units == ledger.charged_units == 5


def test_old_generation_late_receipt_only_changes_old_cost_row(session, sync_seed):
    old, cursor = reserve(session, sync_seed)
    newer, cursor = reserve(session, sync_seed, generation=2)
    record_cost_receipt(session, old.id, uuid4(), total_units=4, closed=True)
    settle_costs(session, cursor)
    assert old.charged_units == 4
    assert (
        newer.generation == 2
        and newer.observed_units == 0
        and newer.charged_units == 50
    )
    assert session.exec(
        select(SyncCostLedger).where(SyncCostLedger.tenant_id == sync_seed.tenant_id)
    ).all()


def test_real_dispatch_reserves_the_unchanged_physical_page_budget(
    session, sync_seed, redis_client
):
    from zoneinfo import ZoneInfo

    from app.modules.reporting import job_tasks  # noqa: F401
    from app.modules.sync_control.dispatcher import dispatch_due

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    for day, tier in ((today, "hot"), (today - timedelta(days=5), "history")):
        enqueue_job(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            frozen_route=_route(sync_seed),
            kind="material_day",
            tier=tier,
            advertiser_ids=["acc-1"],
            start=day,
            end=day,
            due_at=now,
        )
    assert dispatch_due(session, redis_client, now=now, physical_slots=2) == 2
    rows = session.exec(
        select(SyncCostLedger).where(SyncCostLedger.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {row.work_class: row.reserved_units for row in rows} == {
        "history": 50,
        "realtime": 5,
    }
    assert all(
        row.generation == 1 and row.charged_units == row.reserved_units for row in rows
    )


def test_worker_transport_observations_survive_job_rollback_and_keep_endpoint_identity(
    committed_sync_seed,
):
    from types import SimpleNamespace

    from app.core.db import engine
    from app.integrations.tiktok.attempt_observer import physical_attempt_scope
    from app.integrations.tiktok.transport_diagnostics import install_transport_tracking
    from app.modules.sync_control.costs import execution_cost_context

    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        ledger, _ = reserve(session, seed)
        job_id = ledger.report_job_id

    class Connection:
        def request(self):
            return "response"

    class Pool:
        ConnectionCls = Connection

    client = SimpleNamespace(
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(pool_classes_by_scheme={"https": Pool})
        )
    )
    install_transport_tracking(client)
    connection = client.rest_client.pool_manager.pool_classes_by_scheme[
        "https"
    ].ConnectionCls()
    with execution_cost_context(
        engine,
        tenant_id=seed.tenant_id,
        execution_id=job_id,
        generation=1,
        kind="report",
    ):
        with physical_attempt_scope(client, ENDPOINT):
            connection.request()
        with physical_attempt_scope(client, "GET /independent/material/get/"):
            connection.request()
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            job.error_code = "rolled_back"
            session.rollback()
    with factory() as session:
        rows = session.exec(
            select(SyncCostLedger).where(SyncCostLedger.report_job_id == job_id)
        ).all()
        assert {row.physical_endpoint: row.observed_units for row in rows} == {
            ENDPOINT: 1,
            "GET /independent/material/get/": 1,
        }
        assert all(row.closed for row in rows)


def test_report_revocation_before_http_releases_only_queued_occupancy(
    committed_sync_seed, redis_client, monkeypatch
):
    from app.core.config import settings
    from app.core.db import engine
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.reporting import job_tasks
    from app.modules.sync_control.dispatcher import dispatch_due
    from app.modules.sync_control.occupancy import occupancy_keys

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks,
        "_reader_for",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("revoked route cannot open gateway")
        ),
    )
    with factory() as session, session.begin():
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=date(2026, 10, 1),
            end=date(2026, 10, 1),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        assert (
            dispatch_due(session, redis_client, now=datetime.now(UTC), physical_slots=1)
            == 1
        )
        token = session.get(ReportSyncJob, job_id).claim_token
        binding = session.exec(
            select(BCConnectionBinding).where(
                BCConnectionBinding.tenant_id == seed.tenant_id,
                BCConnectionBinding.bc_id == "bc-a",
            )
        ).one()
        binding.revision += 1
    keys = occupancy_keys(
        settings.TIKTOK_APP_ID, ENDPOINT, seed.tenant_id, "acc-1", ENDPOINT
    )
    assert all(redis_client.zscore(key, str(token)) is not None for key in keys)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job_id,
            claim_token=token,
        )
        == "FAILED"
    )
    assert all(redis_client.zscore(key, str(token)) is None for key in keys)


def test_idle_costs_settle_even_when_new_sync_is_disabled(
    committed_sync_seed, monkeypatch
):
    from app.modules.sync_control import tasks

    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        ledger, _ = reserve(session, seed)
        identity = ledger.id
        record_cost_receipt(session, identity, uuid4(), total_units=2, closed=True)
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", False)
    assert tasks.dispatch_sync() == 0
    with factory() as session:
        ledger = session.get(SyncCostLedger, identity)
        assert (
            ledger.charged_units == 2
            and ledger.settled_version == ledger.receipt_version
        )


def test_secondary_cost_commit_failure_retries_same_cumulative_receipt(
    committed_sync_seed,
):
    from sqlalchemy import event

    from app.core.db import engine
    from app.modules.sync_control.costs import _LedgerObserver

    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        ledger, _ = reserve(session, seed)
        session.flush()
        session.expunge(ledger)
    observer = _LedgerObserver(engine, ledger)
    extra = "GET /separate/transport/"
    observer.attempt(extra)

    def fail_commit(_connection):
        raise RuntimeError("synthetic PG commit failure")

    event.listen(engine, "commit", fail_commit)
    try:
        observer.flush()
    finally:
        event.remove(engine, "commit", fail_commit)
    observer.attempt(extra)
    observer.finished = True
    observer.flush()
    with factory() as session:
        row = session.exec(
            select(SyncCostLedger).where(
                SyncCostLedger.report_job_id == ledger.report_job_id,
                SyncCostLedger.physical_endpoint == extra,
            )
        ).one()
        assert row.observed_units == 2 and row.closed


def test_initial_producer_commit_failure_never_enters_http(committed_sync_seed):
    import pytest
    from sqlalchemy import event

    from app.core.db import engine
    from app.modules.sync_control.costs import execution_cost_context

    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        ledger, _ = reserve(session, seed)
        identity, generation = ledger.report_job_id, ledger.generation
    entered = []

    def fail_commit(_connection):
        raise RuntimeError("synthetic producer registration failure")

    event.listen(engine, "commit", fail_commit)
    try:
        with pytest.raises(
            RuntimeError, match="synthetic producer registration failure"
        ):
            with execution_cost_context(
                engine,
                tenant_id=seed.tenant_id,
                execution_id=identity,
                generation=generation,
                kind="report",
            ):
                entered.append("physical request")
    finally:
        event.remove(engine, "commit", fail_commit)
    assert entered == []


def test_unknown_generation_is_sealed_only_after_invalidation_and_all_leases_end(
    session, sync_seed
):
    from app.modules.sync_control.costs import abandon_expired_costs
    from app.modules.sync_control.models import SyncSchedule

    ledger, cursor = reserve(session, sync_seed)
    now = datetime.now(UTC)
    ledger.lease_until = now - timedelta(seconds=1)
    job = session.get(ReportSyncJob, ledger.report_job_id)
    # 单纯超时不证明这一代永远失效。
    assert abandon_expired_costs(session, now=now) == 0
    job.claim_generation = ledger.generation + 1
    job.claimed_until = now + timedelta(seconds=5)
    assert abandon_expired_costs(session, now=now) == 0
    job.claimed_until = None
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    head.lease_execution_id, head.leased_until = job.id, now + timedelta(seconds=5)
    assert abandon_expired_costs(session, now=now) == 0
    head.leased_until = None
    assert abandon_expired_costs(session, now=now) == 1
    assert ledger.abandoned_at == now and not ledger.closed
    settle_costs(session, cursor)
    assert ledger.charged_units == 50
    # 迟到完整收据也不能返还已封存的未知成本，只能增加真实超额。
    producer = uuid4()
    record_cost_receipt(session, ledger.id, producer, total_units=3, closed=True)
    settle_costs(session, cursor)
    assert ledger.charged_units == 50 and ledger.abandoned_at == now
    record_cost_receipt(session, ledger.id, producer, total_units=70, closed=True)
    settle_costs(session, cursor)
    assert ledger.charged_units == 70


def test_unknown_sealer_skips_thirty_two_unexpired_generations_without_starving_terminal(
    session, sync_seed
):
    from app.modules.sync_control.costs import abandon_expired_costs

    ledger, _ = reserve(session, sync_seed)
    now = datetime.now(UTC)
    ledger.lease_until = now - timedelta(minutes=2)
    session.flush()
    for i in range(31):
        session.add(
            SyncCostLedger(
                **(
                    ledger.model_dump()
                    | {
                        "id": uuid4(),
                        "physical_endpoint": f"GET /old-open/{i}/",
                    }
                )
            )
        )
    target_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        kind="ad_day",
        tier="history",
        advertiser_ids=["acc-2"],
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
        frozen_route=_route(sync_seed),
    )
    target = session.get(ReportSyncJob, target_id)
    target.status, target.claim_generation = "COMPLETE", 1
    eligible = SyncCostLedger(
        **(
            ledger.model_dump()
            | {
                "id": uuid4(),
                "report_job_id": target_id,
                "lease_until": now - timedelta(minutes=1),
            }
        )
    )
    session.add(eligible)
    assert abandon_expired_costs(session, now=now, limit=32) == 1
    assert eligible.abandoned_at == now and ledger.abandoned_at is None


@pytest.mark.parametrize("failure_stage", ["abandon", "discover"])
def test_cost_maintenance_database_failure_does_not_stop_dispatch_or_outbox(
    committed_sync_seed,
    monkeypatch,
    failure_stage,
):
    from sqlalchemy import event, text

    from app.core.db import engine
    from app.jobs import tasks as outbox_tasks
    from app.modules.reporting import job_tasks
    from app.modules.sync_control import costs, tasks

    del committed_sync_seed
    calls = []
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_dispatch_pending", lambda *_a, **_kw: calls.append("dispatch") or 1
    )
    monkeypatch.setattr(outbox_tasks, "drain_dispatch", lambda: calls.append("outbox"))

    def broken_abandon(session, **_kw):
        session.execute(text("SELECT 1/0"))

    def broken_discovery(connection, _cursor, statement, _parameters, _context, _many):
        if "SELECT DISTINCT sync_cost_ledger.tenant_id" in statement:
            connection.exec_driver_sql("SELECT 1/0")

    if failure_stage == "abandon":
        monkeypatch.setattr(costs, "abandon_expired_costs", broken_abandon)
    else:
        event.listen(engine, "before_cursor_execute", broken_discovery)
    try:
        assert tasks.dispatch_sync() == 1
        assert calls == ["dispatch", "outbox"]
    finally:
        if failure_stage == "discover":
            event.remove(engine, "before_cursor_execute", broken_discovery)


def test_zero_delta_open_receipt_does_not_block_worker_closing(committed_sync_seed):
    """调度持cursor期间，不应锁住尚无结算差额的在途收据阻止worker收尾。"""
    from sqlalchemy import text

    seed, factory = committed_sync_seed
    producer = uuid4()
    with factory() as session, session.begin():
        ledger, _ = reserve(session, seed, units=1)
        identity = ledger.id
        record_cost_receipt(session, identity, producer, total_units=0, closed=False)
    with factory() as dispatch, dispatch.begin():
        cursor = lock_tenant_cursor(dispatch, seed.tenant_id)
        with factory() as worker, worker.begin():
            worker.execute(text("SET LOCAL lock_timeout='100ms'"))
            record_cost_receipt(worker, identity, producer, total_units=1, closed=True)
        assert settle_costs(dispatch, cursor) == 1
        ledger = dispatch.get(SyncCostLedger, identity, populate_existing=True)
        assert ledger.closed and ledger.charged_units == 1
        assert ledger.settled_version == ledger.receipt_version


def test_open_receipt_exceeding_reservation_still_charges_immediately(
    session, sync_seed
):
    ledger, cursor = reserve(session, sync_seed, units=1)
    record_cost_receipt(session, ledger.id, uuid4(), total_units=7, closed=False)
    assert settle_costs(session, cursor) == 1
    assert ledger.charged_units == 7 and not ledger.closed
    assert ledger.receipt_version == ledger.settled_version
