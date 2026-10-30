"""成本producer首次登记失败：真实PG锁超时不得遗留queued占位或伤害别的消费者。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.modules.ads import tasks
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import balance_tasks
from app.modules.reporting.store_models import ReportBalanceRun
from app.modules.sync_control.balance_demand import (
    ENDPOINT,
    balance_reservation,
    dispatch_balances_due,
)
from app.modules.sync_control.directory_demand import (
    directory_endpoint,
    register_directory_run,
)
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule
from app.modules.sync_control.occupancy import occupancy_keys
from tests.modules.reporting.test_balance_refresh import (
    claim_args,
    claim_balance,
    create_run,
)
from tests.modules.reporting.test_directory_direct_delivery import directory


def prepare(kind, seed, factory, redis):
    if kind == "balance":
        identity = create_run(seed, factory, ["acc-1"])
        payload = claim_balance(seed, factory, redis, identity)
        model = ReportBalanceRun
    else:
        with factory() as db, db.begin():
            run = directory(db, seed)
            register_directory_run(db, run, new_execution=True)
            assert (
                dispatch_directories_due(
                    db, redis, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
                )
                == 1
            )
            identity = run.id
            payload = {"claim_generation": run.claim_generation}
        model = AdDirectoryRun
    return identity, payload, model


def invoke(kind, seed, identity, payload, redis):
    arguments = {
        "database_engine": engine,
        "redis_client": redis,
        "context": seed.context,
        "run_id": identity,
    }
    if kind == "balance":
        return balance_tasks.run_balance(
            **arguments, page=payload["page"], **claim_args(payload)
        )
    return tasks.run_directory_step(
        **arguments, claim_generation=payload["claim_generation"]
    )


def reservation(kind, run):
    from app.modules.ads.directory_dispatch import directory_step_key

    return (
        (balance_reservation(run, run.claim_generation), ENDPOINT)
        if kind == "balance"
        else (directory_step_key(run), directory_endpoint(run))
    )


def snapshot(db, model, identity):
    run = db.get(model, identity, populate_existing=True)
    return (
        run.claim_generation,
        run.claim_token,
        run.claimed_until,
        run.status,
        run.next_page,
        [
            (
                head.scope_id,
                head.work_class,
                head.lease_token,
                head.leased_until,
                head.lease_execution_generation,
            )
            for head in db.exec(
                select(SyncSchedule)
                .where(SyncSchedule.lease_execution_id == identity)
                .order_by(SyncSchedule.scope_id, SyncSchedule.work_class)
            ).all()
        ],
    )


@pytest.mark.parametrize("kind", ["directory", "balance"])
@pytest.mark.parametrize(
    "state", ["queued", "new_generation", "same_generation_worker"]
)
def test_registration_pg_timeout_releases_only_its_unconsumed_queue(
    committed_sync_seed, redis_client, monkeypatch, kind, state
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(tasks, "_deliver_committed_work", lambda *_args: None)
    forbidden = []

    def no_http(**_kwargs):
        forbidden.append(True)
        raise AssertionError("cost登记失败不能进入HTTP")

    monkeypatch.setattr(tasks, "open_tiktok_gateway", no_http)
    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", no_http)
    identity, payload, model = prepare(kind, seed, factory, redis_client)
    with factory() as db:
        run = db.get(model, identity)
        member, endpoint = reservation(kind, run)
        keys = occupancy_keys(
            settings.TIKTOK_APP_ID, endpoint, seed.tenant_id, "acc-1", endpoint
        )
        reference = (
            SyncCostLedger.balance_run_id
            if kind == "balance"
            else SyncCostLedger.directory_run_id
        )
        cost_id = db.exec(
            select(SyncCostLedger.id).where(
                reference == identity,
                SyncCostLedger.generation == payload["claim_generation"],
            )
        ).one()
    assert all(redis_client.zscore(key, member) is not None for key in keys)

    if state == "new_generation":
        with factory() as db, db.begin():
            dispatch = (
                dispatch_balances_due if kind == "balance" else dispatch_directories_due
            )
            assert (
                dispatch(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(minutes=5),
                    limit=1,
                )
                == 1
            )
        with factory() as db:
            next_member, _ = reservation(kind, db.get(model, identity))
        assert next_member != member

    ready, stop = Event(), Event()

    def park_after_claim(session):
        if not current_thread().name.startswith("existing-consumer"):
            return
        if not any(
            isinstance(row, model) and row.id == identity
            for row in session.identity_map.values()
        ):
            return
        # 另一真正消费者已提交A短事务；停在HTTP之前以观察失败的重复消费者。
        ready.set()
        assert stop.wait(10)
        raise RuntimeError("existing consumer stopped before HTTP")

    executor = ThreadPoolExecutor(1, thread_name_prefix="existing-consumer")
    future = None
    if state == "same_generation_worker":
        event.listen(Session, "after_commit", park_after_claim)
        future = executor.submit(invoke, kind, seed, identity, payload, redis_client)
        assert ready.wait(10)
    try:
        with factory() as db:
            before = snapshot(db, model, identity)
        with factory() as blocker, blocker.begin():
            blocker.exec(
                select(SyncCostLedger)
                .where(SyncCostLedger.id == cost_id)
                .with_for_update()
            ).one()
            # observer真实500ms lock_timeout触发SQLAlchemy/PG异常；不替换observer。
            with pytest.raises(DBAPIError):
                invoke(kind, seed, identity, payload, redis_client)
            with factory() as db:
                assert snapshot(db, model, identity) == before
            if state == "queued":
                assert all(redis_client.zscore(key, member) is None for key in keys)
            elif state == "new_generation":
                assert all(
                    redis_client.zscore(key, next_member) is not None for key in keys
                )
            else:
                assert all(redis_client.zscore(key, member) is not None for key in keys)
        assert not forbidden
        if state == "queued":
            with factory() as db, db.begin():
                dispatch = (
                    dispatch_balances_due
                    if kind == "balance"
                    else dispatch_directories_due
                )
                assert (
                    dispatch(
                        db,
                        redis_client,
                        now=datetime.now(UTC) + timedelta(minutes=5),
                        limit=1,
                    )
                    == 1
                )
                recovered = db.get(model, identity)
                assert recovered.claim_generation > payload["claim_generation"]
                recovered_member, _ = reservation(kind, recovered)
            assert all(
                redis_client.zscore(key, recovered_member) is not None for key in keys
            )
    finally:
        stop.set()
        if future is not None:
            with pytest.raises(RuntimeError, match="existing consumer stopped"):
                future.result(timeout=10)
            event.remove(Session, "after_commit", park_after_claim)
        executor.shutdown(wait=True)
