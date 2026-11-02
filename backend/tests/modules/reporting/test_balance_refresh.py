from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlmodel import select

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import AccountBalance
from app.integrations.tiktok.contracts.common import CallEvidence
from app.modules.reporting import balance_tasks, planner
from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.models import AccountBalanceObservation
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import AdSyncBc, ReportBalanceRun
from tests.modules.reporting.test_jobs import D, _route


def create_run(seed, factory, advertisers):
    with factory() as session:
        run = ReportBalanceRun(
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            run_key="balance-test",
            frozen_route=_route(seed),
            remaining_advertiser_ids=advertisers,
        )
        session.add(run)
        session.flush()
        from app.modules.sync_control.balance_demand import register_balance

        register_balance(session, run)
        session.commit()
        return run.id


def claim_balance(seed, factory, redis, run_id):
    from app.jobs.models import PendingDispatch
    from app.modules.sync_control.balance_demand import (
        balance_reservation,
        dispatch_balances_due,
    )

    with factory() as session, session.begin():
        assert dispatch_balances_due(session, redis, now=datetime.now(UTC)) == 1
        run = session.get(ReportBalanceRun, run_id)
        return dict(
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_key
                    == balance_reservation(run, run.claim_generation),
                )
            )
            .one()
            .payload
        )


def claim_args(payload):
    from uuid import UUID

    return {
        "claim_id": UUID(payload["claim_id"]),
        "claim_generation": payload["claim_generation"],
    }


def test_balance_refresh_is_independent_and_visible(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    run_id = create_run(seed, factory, ["acc-1"])
    payload = claim_balance(seed, factory, redis_client, run_id)
    observed = datetime.now(UTC)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(
            ads=SimpleNamespace(
                read_balance_page=lambda advs, page: (
                    {
                        advs[0]: AccountBalance(
                            Decimal("12.50"),
                            "USD",
                            "AVAILABLE",
                            observed,
                            CallEvidence(),
                            "ADVERTISER",
                            advs[0],
                        )
                    },
                    None,
                )
            )
        )

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert (
        balance_tasks.run_balance(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=run_id,
            page=1,
            **claim_args(payload),
        )
        == "COMPLETE"
    )
    with factory() as s:
        assert s.exec(
            select(AccountBalanceObservation).where(
                AccountBalanceObservation.tenant_id == seed.tenant_id
            )
        ).one().amount == Decimal("12.50")
        rows = build_rows(
            s,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(dimension="account", start_date=D, end_date=D),
        )
        row = next(r for r in rows if r.display["advertiser_id"] == "acc-1")
        assert row.display["balance"] == "12.50"
        assert row.display["balance_currency"] == "USD"
        assert row.display["balance_scope"] == "ADVERTISER"
        assert row.metric_buckets[0].values["spend"] is None


def test_balance_plan_coalesces_per_half_hour(session, sync_seed, monkeypatch):
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    assert balance_tasks.enqueue_balances(session, plan, ["acc-1"], now) == 1
    assert balance_tasks.enqueue_balances(session, plan, ["acc-1"], now) == 0


def test_new_balance_batches_follow_each_account_owner_without_upgrading_old_run(
    session, sync_seed, monkeypatch, redis_client
):
    from datetime import timedelta

    from app.modules.sync_control.models import SyncScope

    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    assert (
        balance_tasks.enqueue_balances(
            session, plan, ["acc-1", "acc-2"], now - timedelta(hours=1)
        )
        == 1
    )
    old = session.exec(
        select(ReportBalanceRun).where(
            ReportBalanceRun.tenant_id == sync_seed.tenant_id
        )
    ).one()
    scope = session.exec(
        select(SyncScope).where(
            SyncScope.tenant_id == sync_seed.tenant_id,
            SyncScope.advertiser_id == "acc-1",
        )
    ).one()
    scope.owner_epoch = 2
    session.flush()
    assert balance_tasks.enqueue_balances(session, plan, ["acc-1", "acc-2"], now) == 2
    runs = session.exec(
        select(ReportBalanceRun).where(
            ReportBalanceRun.tenant_id == sync_seed.tenant_id,
            ReportBalanceRun.id != old.id,
        )
    ).all()
    assert {(tuple(run.remaining_advertiser_ids), run.owner_epoch) for run in runs} == {
        (("acc-1",), 2),
        (("acc-2",), 1),
    }
    assert old.owner_epoch == 1
    from app.jobs.models import PendingDispatch
    from app.modules.sync_control.balance_demand import dispatch_balances_due

    assert (
        dispatch_balances_due(
            session, redis_client, now=now + timedelta(seconds=1), limit=2
        )
        == 2
    )
    messages = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == "reporting.balance_step",
        )
    ).all()
    assert {message.payload["run_id"] for message in messages} == {
        str(run.id) for run in runs
    }


def test_balance_batch_heads_do_not_hide_another_tenant(
    session, sync_seed, monkeypatch, redis_client
):
    from app.jobs.models import PendingDispatch
    from app.modules.sync_control.balance_demand import dispatch_balances_due
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    other = seed_fixture.__wrapped__(session)
    now = datetime.now(UTC)
    for seed, accounts in ((sync_seed, ["acc-1", "acc-2"]), (other, ["acc-1"])):
        plan = SimpleNamespace(
            tenant_id=seed.tenant_id, actor_id=seed.actor_id, frozen_route=_route(seed)
        )
        balance_tasks.enqueue_balances(session, plan, accounts, now)
    assert (
        dispatch_balances_due(
            session,
            redis_client,
            now=now + __import__("datetime").timedelta(seconds=1),
            limit=2,
        )
        == 2
    )
    messages = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "reporting.balance_step",
            PendingDispatch.tenant_id.in_([sync_seed.tenant_id, other.tenant_id]),
        )
    ).all()
    assert {message.tenant_id for message in messages} == {
        sync_seed.tenant_id,
        other.tenant_id,
    }


def test_disabled_balance_never_opens_gateway(monkeypatch, sync_seed):
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", False)
    assert (
        balance_tasks.run_balance(
            database_engine=None,
            redis_client=None,
            context=sync_seed.context,
            run_id=None,
            page=1,
            claim_id=None,
            claim_generation=0,
        )
        == "WAITING"
    )


def test_balance_schedule_ignores_report_backoff_and_coalesces(
    monkeypatch, committed_sync_seed
):
    from datetime import timedelta

    from app.jobs.celery_app import celery_app
    from app.jobs.models import PendingDispatch

    seed, factory = committed_sync_seed
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime(2026, 10, 12, 13, tzinfo=UTC)
    with factory() as s:
        planner.ensure_bc_plans(s, now=now)
        for plan in s.exec(
            select(AdSyncBc).where(AdSyncBc.tenant_id == seed.tenant_id)
        ):
            plan.next_hot_at = plan.next_daily_at = plan.next_material_at = (
                plan.next_directory_at
            ) = now + timedelta(days=1)
        s.commit()

    def own_runs():
        # 共享测试库中其他用例可能留下已启用计划；只统计本租户的余额运行。
        with factory() as s:
            return len(
                s.exec(
                    select(ReportBalanceRun.id).where(
                        ReportBalanceRun.tenant_id == seed.tenant_id
                    )
                ).all()
            )

    balance_tasks.plan_balances(database_engine=engine, now=now)
    assert own_runs() == 1
    balance_tasks.plan_balances(database_engine=engine, now=now)
    assert own_runs() == 1
    balance_tasks.plan_balances(database_engine=engine, now=now + timedelta(minutes=30))
    assert own_runs() == 1
    with factory() as s:
        rows = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == balance_tasks.TASK_NAME,
            )
        ).all()
        assert rows == []
        from app.modules.sync_control.models import SyncSchedule

        assert s.exec(
            select(SyncSchedule).where(
                SyncSchedule.tenant_id == seed.tenant_id,
                SyncSchedule.work_class == "maintenance",
            )
        ).all()
    entries = [
        e
        for e in celery_app.conf.beat_schedule.values()
        if e["task"] == "reporting.plan_balances"
    ]
    assert len(entries) == 1 and entries[0]["schedule"] == 1800


def test_balance_page_commits_observations_with_next_page(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.jobs.models import PendingDispatch

    seed, factory = committed_sync_seed
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    calls = []

    @contextmanager
    def gateway(**_kwargs):
        def read(advs, *, page):
            calls.append((advs, page))
            adv = "acc-1" if page == 1 else "acc-2"
            return {
                adv: AccountBalance(
                    Decimal("8"),
                    "USD",
                    "AVAILABLE",
                    datetime.now(UTC),
                    CallEvidence(),
                    "ADVERTISER",
                    adv,
                )
            }, 2 if page == 1 else None

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    args = {
        "database_engine": engine,
        "redis_client": redis_client,
        "context": seed.context,
        "run_id": create_run(seed, factory, ["acc-1", "acc-2"]),
    }
    initial_payload = claim_balance(seed, factory, redis_client, args["run_id"])
    assert (
        balance_tasks.run_balance(**args, page=1, **claim_args(initial_payload))
        == "CONTINUE"
    )
    with factory() as s, s.begin():
        from app.modules.sync_control.balance_demand import dispatch_balances_due

        assert dispatch_balances_due(s, redis_client, now=datetime.now(UTC)) == 1
        pending = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == balance_tasks.TASK_NAME,
                PendingDispatch.payload["page"].as_integer() == 2,
            )
        ).one()
        payload = pending.payload
        assert payload["page"] == 2 and payload["run_id"] == str(args["run_id"])
        assert s.get(ReportBalanceRun, args["run_id"]).remaining_advertiser_ids == [
            "acc-2"
        ]
        assert (
            s.exec(
                select(AccountBalanceObservation).where(
                    AccountBalanceObservation.tenant_id == seed.tenant_id
                )
            )
            .one()
            .advertiser_id
            == "acc-1"
        )
    assert (
        balance_tasks.run_balance(**args, page=payload["page"], **claim_args(payload))
        == "COMPLETE"
    )
    assert (
        balance_tasks.run_balance(**args, page=1, **claim_args(initial_payload))
        == "IGNORED"
    )
    assert calls == [(("acc-1", "acc-2"), 1), (("acc-2",), 2)]


def test_large_balance_scope_stays_out_of_bounded_queue_payload(
    session, sync_seed, monkeypatch, redis_client
):
    import json

    from app.jobs.models import PendingDispatch

    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    accounts = [str(7000000000000000000 + i) for i in range(2000)]
    from app.modules.accounts.models import AdvertiserAccount

    session.add_all(
        [
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=account,
                timezone="UTC",
                currency="USD",
            )
            for account in accounts
        ]
    )
    session.flush()
    assert balance_tasks.enqueue_balances(session, plan, accounts, now) == 1
    from sqlalchemy import event, text

    from app.modules.sync_control.balance_demand import dispatch_balances_due

    session.execute(text("SET LOCAL statement_timeout='5s'"))
    session.execute(text("SET LOCAL plan_cache_mode=force_generic_plan"))
    plans = []
    connection = session.connection()

    def capture(_conn, cursor, statement, parameters, _context, _many):
        if "locked_balance" in statement:
            cursor.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, parameters
            )
            plans.append(cursor.fetchone()[0][0])

    driver = connection.connection.driver_connection
    old_threshold = driver.prepare_threshold
    driver.prepare_threshold = 0
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert dispatch_balances_due(session, redis_client, now=datetime.now(UTC)) == 1
    finally:
        event.remove(connection, "before_cursor_execute", capture)
        driver.prepare_threshold = old_threshold
        print("BALANCE_DISPATCH_PLANS " + json.dumps(plans, default=str))  # noqa: T201

    def plan_nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from plan_nodes(child)

    owner_probes = [
        node
        for plan in plans
        for node in plan_nodes(plan["Plan"])
        if node.get("Alias") == "sync_scope_1"
    ]
    assert owner_probes and all(
        node["Actual Loops"] <= 4000 for node in owner_probes
    ), owner_probes
    pending = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == balance_tasks.TASK_NAME,
        )
    ).one()
    assert len(json.dumps(pending.payload)) < 200
    assert set(pending.payload) == {"run_id", "page", "claim_id", "claim_generation"}
    from uuid import UUID

    assert (
        session.get(
            ReportBalanceRun, UUID(pending.payload["run_id"])
        ).remaining_advertiser_ids
        == accounts
    )


def test_late_balance_error_cannot_fail_reclaimed_generation(
    monkeypatch, committed_sync_seed, redis_client
):
    from contextlib import nullcontext
    from uuid import UUID

    from app.core.errors import DomainError
    from tests.modules.reporting.test_balance_claims import expire_claim, invoke

    seed, factory = committed_sync_seed
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        balance_tasks.Redis, "from_url", lambda *_a, **_k: nullcontext(redis_client)
    )
    run_id = create_run(seed, factory, ["acc-1"])
    payload = claim_balance(seed, factory, redis_client, run_id)
    replacement = {}

    @contextmanager
    def gateway(**_kwargs):
        def read(*_args, **_kwargs):
            expire_claim(factory, run_id)
            replacement.update(claim_balance(seed, factory, redis_client, run_id))
            raise DomainError("authorization_revoked", "late transport failure")

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert invoke(seed, payload) == "IGNORED"
    with factory() as session:
        run = session.get(ReportBalanceRun, run_id)
        assert run.status == "ACTIVE" and run.claim_token == UUID(
            replacement["claim_id"]
        )


def test_balance_outcome_updates_many_account_heads_in_bounded_queries(
    session, sync_seed, monkeypatch
):
    from datetime import timedelta

    from sqlalchemy import event, insert

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.sync_control.balance_demand import balance_outcome
    from app.modules.sync_control.models import SyncSchedule

    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    accounts = [f"outcome-{i}" for i in range(200)]
    session.connection().execute(
        insert(AdvertiserAccount),
        [
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=account,
                timezone="UTC",
                currency="USD",
            ).model_dump()
            for account in accounts
        ],
    )
    assert (
        balance_tasks.enqueue_balances(
            session, session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a")), accounts, now
        )
        == 1
    )
    run = session.exec(
        select(ReportBalanceRun).where(
            ReportBalanceRun.tenant_id == sync_seed.tenant_id
        )
    ).one()
    due = now + timedelta(minutes=1)
    run.next_attempt_at = due
    session.flush()
    queries = []

    def count(_conn, _cursor, statement, _params, _context, _many):
        queries.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", count)
    try:
        balance_outcome(session, run)
        session.flush()
    finally:
        event.remove(connection, "before_cursor_execute", count)
    heads = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(heads) == 200 and all(
        head.next_eligible_at == due and head.active for head in heads
    )
    assert len(queries) < 10


def test_periodic_balance_reuses_pending_accounts_across_many_ticks(
    session, sync_seed, monkeypatch
):
    from datetime import timedelta

    from app.modules.sync_control.balance_demand import balance_outcome
    from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement

    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    assert balance_tasks.enqueue_balances(session, plan, ["acc-1"], now) == 1
    run = session.exec(select(ReportBalanceRun)).one()
    run.next_page = 7
    run.claim_generation = 1
    run.claimed_until = now + timedelta(minutes=40)
    child = session.exec(select(SyncExecutionScope)).one()
    child.execution_generation = 1
    frozen = (child.frozen_revision, child.frozen_requested_after)
    for tick in range(1, 61):
        assert (
            balance_tasks.enqueue_balances(
                session, plan, ["acc-1"], now + timedelta(minutes=30 * tick)
            )
            == 0
        )
    assert run.next_page == 7 and run.claim_generation == 1
    assert (child.frozen_revision, child.frozen_requested_after) == frozen
    requirement = session.exec(select(SyncRequirement)).one()
    assert requirement.desired_revision == 1
    assert requirement.satisfied_revision == 0
    # 新加入账户不能被整批去重遗漏。
    later = now + timedelta(hours=31)
    assert balance_tasks.enqueue_balances(session, plan, ["acc-1", "acc-2"], later) == 1
    assert session.exec(
        select(ReportBalanceRun).where(ReportBalanceRun.id != run.id)
    ).one().remaining_advertiser_ids == ["acc-2"]
    # 已结束的需求不会阻止下一周期真正刷新。
    run.status = "COMPLETE"
    run.claimed_until = None
    balance_outcome(session, run)
    assert (
        balance_tasks.enqueue_balances(
            session, plan, ["acc-1", "acc-2"], later + timedelta(minutes=30)
        )
        == 1
    )
