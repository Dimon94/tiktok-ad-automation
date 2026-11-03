"""同租户不同物理接口的领取，不应互相占住公平账本行。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.schedule import report_endpoint
from tests.modules.reporting.test_jobs import _route
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_idle_settlement_uses_same_endpoint_protocol(committed_sync_seed):
    from app.modules.sync_control.costs import settle_idle_costs
    from app.modules.sync_control.dispatch_budget import EndpointBudgets
    from app.modules.sync_control.models import SyncCostLedger

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    endpoint = report_endpoint("material_day")
    with factory() as db, db.begin():
        jobs = [
            enqueue_job(
                db,
                tenant_id=seed.tenant_id,
                actor_id=seed.actor_id,
                bc_id="bc-a",
                kind="material_day",
                tier="hot",
                advertiser_ids=[advertiser],
                start=now.date(),
                end=now.date(),
                frozen_route=_route(seed),
                due_at=now,
            )
            for advertiser in ["acc-1", "acc-2"]
        ]
        db.add(
            ReportDispatchCursor(
                tenant_id=seed.tenant_id,
                last_dispatched_at=now,
                fairness_state={endpoint: {"realtime": 0, "history": 0}},
            )
        )
        db.add(
            SyncCostLedger(
                tenant_id=seed.tenant_id,
                report_job_id=jobs[0],
                generation=1,
                physical_endpoint=endpoint,
                work_class="realtime",
                eligible_weights={"realtime": 50, "history": 30},
                endpoint_weights={},
                reserved_units=5,
                charged_units=5,
                observed_units=1,
                closed=True,
                receipt_version=1,
                settled_version=0,
                lease_until=now + timedelta(minutes=1),
            )
        )
        engine = db.get_bind()
    with factory() as dispatch, dispatch.begin():
        budgets = EndpointBudgets(dispatch)
        assert budgets.acquire(seed.tenant_id, endpoint) is not None
        assert budgets.settled == 1
        # 新收据在调度读取余额之后提交，idle不能绕过同接口协议抢先结算。
        with factory() as producer, producer.begin():
            producer.add(
                SyncCostLedger(
                    tenant_id=seed.tenant_id,
                    report_job_id=jobs[1],
                    generation=1,
                    physical_endpoint=endpoint,
                    work_class="realtime",
                    eligible_weights={"realtime": 50, "history": 30},
                    endpoint_weights={},
                    reserved_units=2,
                    charged_units=2,
                    observed_units=1,
                    closed=True,
                    receipt_version=1,
                    settled_version=0,
                    lease_until=now + timedelta(minutes=1),
                )
            )
        assert settle_idle_costs(engine, budget_seconds=2) == 0
        budgets.persist()
    assert settle_idle_costs(engine, budget_seconds=2) == 1
    with factory() as db:
        state = db.get(ReportDispatchCursor, seed.tenant_id).fairness_state[endpoint]
        assert state["realtime"] == 150 and state["history"] == -150


def test_outer_pg_commit_failure_releases_successful_inner_reservation(
    committed_sync_seed, redis_client
):
    from uuid import uuid4

    from app.modules.sync_control.dispatch_budget import dispatch_budget_transaction
    from app.modules.sync_control.occupancy import (
        occupancy_keys,
        reservation_transaction,
        reserve_dispatch,
    )
    from tests.modules.sync_control.test_endpoint_readiness import _policy, _scope

    _, factory = committed_sync_seed
    scope = _scope()
    with pytest.raises(IntegrityError):
        with reservation_transaction(redis_client):
            with factory() as db, db.begin():
                db.execute(
                    text("CREATE TEMP TABLE budget_commit_parent(id int PRIMARY KEY)")
                )
                db.execute(
                    text(
                        "CREATE TEMP TABLE budget_commit_child(id int REFERENCES budget_commit_parent(id) DEFERRABLE INITIALLY DEFERRED)"
                    )
                )
                with dispatch_budget_transaction(db, redis_client):
                    assert (
                        reserve_dispatch(
                            redis_client,
                            reservation_id=uuid4(),
                            policy=_policy(),
                            **scope,
                        ).capacity_hint
                        > 0
                    )
                    db.execute(text("INSERT INTO budget_commit_child VALUES(1)"))
                # 保存点成功；故意缺失父行，真实PostgreSQL在最外层COMMIT拒绝。
    assert all(redis_client.zcard(key) == 0 for key in occupancy_keys(**scope))


def test_legacy_same_endpoint_update_rolls_back_prepared_claim_and_reservation(
    committed_sync_seed, redis_client
):
    from sqlmodel import select

    from app.core.config import settings
    from app.jobs.models import DispatchTenantCursor, PendingDispatch
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.models import SyncCostLedger
    from app.modules.sync_control.occupancy import occupancy_keys

    seed, factory = committed_sync_seed
    endpoint = "GET /open_api/v1.3/smart_plus/ad/get/"
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        run = _directory(db, seed)
        run.workload = "live"
        run_id = run.id
        db.add(
            ReportDispatchCursor(
                tenant_id=seed.tenant_id, last_dispatched_at=now, fairness_state={}
            )
        )
        db.add(DispatchTenantCursor(tenant_id=seed.tenant_id))
        engine = db.get_bind()
    prepared, release = Event(), Event()

    def pause(_conn, _cursor, statement, _params, _ctx, _many):
        if current_thread().name.startswith(
            "legacy-budget"
        ) and statement.lower().lstrip().startswith("insert into pending_dispatch"):
            prepared.set()
            assert release.wait(10)

    def dispatch():
        with factory() as db, db.begin():
            return dispatch_directories_due(
                db, redis_client, now=now + timedelta(seconds=1), limit=4, max_claims=1
            )

    event.listen(engine, "after_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="legacy-budget"
        ) as workers:
            result = workers.submit(dispatch)
            try:
                assert prepared.wait(10)
                # 模拟尚未退出的旧版本：它只锁tenant行，不认识新endpoint advisory。
                with factory() as old, old.begin():
                    old.execute(text("SET LOCAL lock_timeout='200ms'"))
                    cursor = old.get(
                        ReportDispatchCursor, seed.tenant_id, with_for_update=True
                    )
                    cursor.fairness_state = {endpoint: {"directory": 1234.0}}
            finally:
                release.set()
            assert result.result(timeout=10) == 0
        with factory() as db:
            assert db.get(AdDirectoryRun, run_id).claim_generation == 1
            assert not db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id
                )
            ).all()
            assert not db.exec(
                select(SyncCostLedger).where(SyncCostLedger.tenant_id == seed.tenant_id)
            ).all()
            assert db.get(ReportDispatchCursor, seed.tenant_id).fairness_state[
                endpoint
            ] == {"directory": 1234.0}
        scope = {
            "app_scope": settings.TIKTOK_APP_ID,
            "endpoint": endpoint,
            "rate_group": endpoint,
            "tenant_id": seed.tenant_id,
            "advertiser_id": "acc-1",
        }
        assert all(redis_client.zcard(key) == 0 for key in occupancy_keys(**scope))
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", pause)


def test_existing_outbox_cursor_update_does_not_block_new_messages(committed_sync_seed):
    from uuid import uuid4

    from app.jobs.models import DispatchTenantCursor
    from app.jobs.outbox import enqueue_after_commit
    from app.modules.reporting import job_tasks  # noqa: F401

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        db.add(DispatchTenantCursor(tenant_id=seed.tenant_id))
    with factory() as sender, sender.begin():
        cursor = sender.get(DispatchTenantCursor, seed.tenant_id, with_for_update=True)
        cursor.last_published_at = datetime.now(UTC)
        sender.flush()
        with factory() as producer, producer.begin():
            producer.execute(text("SET LOCAL statement_timeout='200ms'"))
            assert enqueue_after_commit(
                producer,
                context=seed.context,
                task_name="jobs.probe",
                task_key=f"endpoint-budget:{uuid4()}",
                payload={},
            )


def test_other_endpoint_can_commit_while_directory_prepares_next_claim(
    committed_sync_seed, redis_client
):
    from app.jobs.models import DispatchTenantCursor
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.reporting import job_tasks  # noqa: F401

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        for advertiser in ["acc-1", "acc-2"]:
            run = _directory(db, seed, advertiser=advertiser)
            run.workload = "live"
        enqueue_job(
            db,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            bc_id="bc-a",
            kind="material_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=now.date(),
            end=now.date(),
            frozen_route=_route(seed),
            due_at=now,
        )
        db.add(
            ReportDispatchCursor(
                tenant_id=seed.tenant_id, last_dispatched_at=now, fairness_state={}
            )
        )
        db.add(DispatchTenantCursor(tenant_id=seed.tenant_id))
        engine = db.get_bind()
    prepared, release = Event(), Event()

    def pause_after_first_message(_conn, _cursor, statement, _params, _ctx, _many):
        if (
            current_thread().name.startswith("directory-endpoint")
            and statement.lower().lstrip().startswith("insert into pending_dispatch")
            and not prepared.is_set()
        ):
            prepared.set()
            assert release.wait(10), "contender did not finish"

    def directory_turn():
        with factory() as db, db.begin():
            return dispatch_directories_due(
                db,
                redis_client,
                now=now + timedelta(seconds=1),
                limit=4,
                max_claims=2,
            )

    event.listen(engine, "after_cursor_execute", pause_after_first_message)
    try:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="directory-endpoint"
        ) as workers:
            pending = workers.submit(directory_turn)
            try:
                assert prepared.wait(10), "first directory claim not prepared"
                with factory() as db, db.begin():
                    count = dispatch_due(
                        db,
                        redis_client,
                        now=now + timedelta(seconds=2),
                        physical_slots=4,
                        max_claims=1,
                    )
                assert count == 1
            finally:
                release.set()
                assert pending.result(timeout=10) == 2
        with factory() as db:
            cursor = db.get(ReportDispatchCursor, seed.tenant_id)
            assert report_endpoint("material_day") in cursor.fairness_state
            assert "GET /open_api/v1.3/smart_plus/ad/get/" in cursor.fairness_state
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", pause_after_first_message)
