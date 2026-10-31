"""余额消息持有派发身份；重复消费、迟到异常和重试均受持久围栏约束。"""

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.reporting import balance_tasks
from app.modules.reporting.store_models import ReportBalanceRun
from app.modules.sync_control.balance_demand import (
    balance_outcome,
    dispatch_balances_due,
    register_balance,
)
from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.reporting.test_jobs import _route


@pytest.fixture(autouse=True)
def enabled(monkeypatch, redis_client):
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        balance_tasks.Redis, "from_url", lambda *_a, **_k: nullcontext(redis_client)
    )


def create_claim(seed, factory, redis, *, run_id=None, now=None):
    now = now or datetime.now(UTC)
    with factory() as db, db.begin():
        if run_id is None:
            run = ReportBalanceRun(
                tenant_id=seed.tenant_id,
                actor_id=seed.actor_id,
                run_key=uuid4().hex,
                frozen_route=_route(seed),
                remaining_advertiser_ids=["acc-1"],
            )
            db.add(run)
            db.flush()
            register_balance(db, run)
            run_id = run.id
        assert dispatch_balances_due(db, redis, now=now + timedelta(seconds=1)) == 1
        run = db.get(ReportBalanceRun, UUID(str(run_id)))
        from app.modules.sync_control.balance_demand import balance_reservation

        message = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == balance_tasks.TASK_NAME,
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key
                == balance_reservation(run, run.claim_generation),
            )
        ).one()
        return dict(message.payload)


def invoke(seed, payload):
    from app.modules.sync_control.dispatch_envelope import dispatch_envelope

    with Session(balance_tasks.engine) as db:
        message = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == balance_tasks.TASK_NAME,
                PendingDispatch.payload == payload,
            )
        ).first()
        envelope = dispatch_envelope(message) if message is not None else None
    return balance_tasks.balance_step.run(
        tenant_id=str(seed.tenant_id),
        actor_id=str(seed.actor_id),
        payload=payload,
        dispatch=envelope,
    )


def expire_claim(factory, run_id, now=None):
    now = now or datetime.now(UTC)
    with factory() as db, db.begin():
        run = db.get(ReportBalanceRun, UUID(str(run_id)), with_for_update=True)
        run.claimed_until = now - timedelta(seconds=1)
        for head in db.exec(
            select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
        ):
            head.leased_until = now - timedelta(seconds=1)


def test_dispatch_persists_claim_before_worker_authorization(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)
    assert set(payload) == {"run_id", "page", "claim_id", "claim_generation"}
    with factory() as db:
        run = db.get(ReportBalanceRun, UUID(payload["run_id"]))
        child = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.balance_run_id == run.id
            )
        ).one()
        assert (
            run.claim_generation
            == payload["claim_generation"]
            == child.execution_generation
        )
        assert str(run.claim_token) == payload[
            "claim_id"
        ] and run.claimed_until > datetime.now(UTC)
        assert child.observed_at is None


def test_verify_route_revocation_retires_dispatched_generation(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)
    from app.jobs.admission import admission_keys, admission_policy, admit_call
    from app.modules.sync_control.balance_demand import ENDPOINT
    from app.modules.sync_control.occupancy import occupancy_keys

    scope = {
        "app_scope": balance_tasks.settings.TIKTOK_APP_ID,
        "endpoint": ENDPOINT,
        "rate_group": ENDPOINT,
        "tenant_id": seed.tenant_id,
        "advertiser_id": "acc-1",
    }
    queued = (
        f"balance:{payload['run_id']}:{payload['claim_generation']}:{payload['page']}"
    )
    assert all(
        redis_client.zscore(key, queued) is not None for key in occupancy_keys(**scope)
    )
    # 另一个已发送调用的真实准入账本不能被撤权清理退款。
    policy = admission_policy(
        "finance.get_advertiser_balances",
        channel="OFFICIAL_API",
        physical_endpoint=ENDPOINT,
    ).model_copy(update={"endpoint_calls_per_day": 1000})
    sent = uuid4()
    assert admit_call(redis_client, lease_id=sent, policy=policy, **scope).granted
    sent_keys = admission_keys(**scope)
    day_key = sent_keys[1] + ":day"
    day_spent = redis_client.hgetall(day_key)
    assert day_spent

    def revoke(*_args, **_kwargs):
        raise DomainError("authorization_revoked", "test revoked before HTTP")

    monkeypatch.setattr(balance_tasks, "verify_read_routes", revoke)
    monkeypatch.setattr(
        balance_tasks,
        "open_tiktok_gateway",
        lambda **_: pytest.fail("revoked claim opened HTTP"),
    )
    assert invoke(seed, payload) == "FAILED"
    assert all(
        redis_client.zscore(key, queued) is None for key in occupancy_keys(**scope)
    )
    assert redis_client.hgetall(day_key) == day_spent
    assert all(
        redis_client.zscore(key, str(sent)) is not None for key in sent_keys[2:6]
    )
    with factory() as db:
        run = db.get(ReportBalanceRun, UUID(payload["run_id"]))
        assert run.status == "FAILED" and run.claim_token is None
        assert (
            db.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.balance_run_id == run.id
                )
            )
            .one()
            .status
            == "SUPERSEDED"
        )
        heads = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).all()
        assert all(h.lease_token is None and not h.active for h in heads)


def test_old_message_after_new_dispatch_never_opens_http(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    old = create_claim(seed, factory, redis_client)
    expire_claim(factory, old["run_id"])
    new = create_claim(seed, factory, redis_client, run_id=old["run_id"])
    monkeypatch.setattr(
        balance_tasks,
        "open_tiktok_gateway",
        lambda **_: pytest.fail("old message opened HTTP"),
    )
    assert invoke(seed, old) == "IGNORED"
    with factory() as db:
        run = db.get(ReportBalanceRun, UUID(old["run_id"]))
        assert str(run.claim_token) == new["claim_id"]
        assert run.claim_generation == new["claim_generation"]


def test_old_http_failure_cannot_fail_new_queued_claim(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    old = create_claim(seed, factory, redis_client)
    latest = {}

    @contextmanager
    def gateway(**_kwargs):
        def read(*_args, **_kwargs):
            expire_claim(factory, old["run_id"])
            latest.update(
                create_claim(seed, factory, redis_client, run_id=old["run_id"])
            )
            raise DomainError("authorization_revoked", "late old HTTP error")

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert invoke(seed, old) == "IGNORED"
    from app.modules.sync_control.balance_demand import ENDPOINT
    from app.modules.sync_control.occupancy import occupancy_keys

    keys = occupancy_keys(
        balance_tasks.settings.TIKTOK_APP_ID,
        ENDPOINT,
        seed.tenant_id,
        "acc-1",
        ENDPOINT,
    )
    old_member = f"balance:{old['run_id']}:{old['claim_generation']}:{old['page']}"
    new_member = (
        f"balance:{latest['run_id']}:{latest['claim_generation']}:{latest['page']}"
    )
    assert all(redis_client.zscore(key, old_member) is None for key in keys)
    assert all(redis_client.zscore(key, new_member) is not None for key in keys)
    with factory() as db:
        run = db.get(ReportBalanceRun, UUID(old["run_id"]))
        assert run.status == "ACTIVE" and str(run.claim_token) == latest["claim_id"]
        assert run.claim_generation == latest["claim_generation"]
        assert (
            db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
            )
            .one()
            .lease_execution_generation
            == run.claim_generation
        )


def test_duplicate_queued_token_during_http_does_not_call_provider(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)
    calls = []

    @contextmanager
    def gateway(**_kwargs):
        def read(*_args, **_kwargs):
            calls.append(1)
            assert invoke(seed, payload) == "IGNORED"
            return {}, None

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert invoke(seed, payload) == "WAITING"
    assert calls == [1]


def test_provider_retry_budget_survives_new_dispatches(
    monkeypatch, committed_sync_seed, redis_client
):
    import app.modules.sync_control.balance_demand as demand

    seed, factory = committed_sync_seed
    clock = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(balance_tasks, "datetime", Clock)
    monkeypatch.setattr(demand, "datetime", Clock)

    @contextmanager
    def gateway(**_kwargs):
        def read(*_args, **_kwargs):
            raise DomainError("tiktok_response_error", "provider retry")

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    payload = create_claim(seed, factory, redis_client, now=clock[0])
    generations = []
    for attempt in range(1, 7):
        generations.append(payload["claim_generation"])
        result = invoke(seed, payload)
        with factory() as db:
            run = db.get(ReportBalanceRun, UUID(payload["run_id"]))
            assert run.provider_failures == attempt
            assert run.claim_token is None and run.claimed_until is None
            if attempt < 6:
                assert result == "WAITING" and run.status == "ACTIVE"
                assert run.next_attempt_at > clock[0]
                clock[0] = run.next_attempt_at + timedelta(seconds=1)
            else:
                assert result == "FAILED" and run.status == "FAILED"
        if attempt < 6:
            assert invoke(seed, payload) == "IGNORED"
            payload = create_claim(
                seed, factory, redis_client, run_id=payload["run_id"], now=clock[0]
            )
    assert generations == sorted(set(generations))


def test_old_payload_is_rejected_without_http(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)
    monkeypatch.setattr(
        balance_tasks,
        "open_tiktok_gateway",
        lambda **_: pytest.fail("legacy payload opened HTTP"),
    )
    assert invoke(seed, {key: payload[key] for key in ("run_id", "page")}) == "IGNORED"
    with factory() as db:
        run = db.get(ReportBalanceRun, UUID(payload["run_id"]))
        assert str(run.claim_token) == payload["claim_id"] and run.status == "ACTIVE"


def test_admission_wait_keeps_provider_budget_and_page(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)

    @contextmanager
    def gateway(**_kwargs):
        def read(*_args, **_kwargs):
            raise AccountAdmissionDeferred(30000)

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert invoke(seed, payload) == "WAITING"
    with factory() as db:
        run = db.get(ReportBalanceRun, UUID(payload["run_id"]))
        assert run.provider_failures == 0 and run.next_page == 1
        assert run.next_attempt_at > datetime.now(UTC)
        assert run.claim_token is None and run.remaining_advertiser_ids == ["acc-1"]
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        assert (
            head.active
            and head.leased_until is None
            and head.next_eligible_at == run.next_attempt_at
        )


def test_stale_loaded_head_cannot_clear_new_dispatch_lease(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)
    with factory() as observer, observer.begin():
        old = observer.get(ReportBalanceRun, UUID(payload["run_id"]))
        stale = observer.exec(
            select(SyncSchedule).where(SyncSchedule.lease_execution_id == old.id)
        ).one()
        with factory() as writer, writer.begin():
            finished = writer.get(ReportBalanceRun, old.id, with_for_update=True)
            finished.status = "FAILED"
            balance_outcome(writer, finished)
            newer = ReportBalanceRun(
                tenant_id=seed.tenant_id,
                actor_id=seed.actor_id,
                run_key=uuid4().hex,
                frozen_route=_route(seed),
                remaining_advertiser_ids=["acc-1"],
            )
            writer.add(newer)
            writer.flush()
            register_balance(writer, newer)
            assert (
                dispatch_balances_due(
                    writer, redis_client, now=datetime.now(UTC) + timedelta(seconds=1)
                )
                == 1
            )
            replacement = (newer.id, newer.claim_generation, newer.claim_token)
        balance_outcome(observer, old)
        observer.flush()
        assert stale.lease_execution_id == replacement[0]
        assert stale.lease_execution_generation == replacement[1]
    with factory() as db:
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        assert (
            head.lease_execution_id == replacement[0] and head.lease_token is not None
        )


def test_outcome_locks_head_until_new_dispatch_can_replace_it(
    committed_sync_seed, redis_client
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from sqlalchemy import event, text

    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)
    expire_claim(factory, payload["run_id"])
    with factory() as db, db.begin():
        newer = ReportBalanceRun(
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            run_key=uuid4().hex,
            frozen_route=_route(seed),
            remaining_advertiser_ids=["acc-1"],
        )
        db.add(newer)
        db.flush()
        register_balance(db, newer)
        newer_id = newer.id
    started = Event()
    writer_pid = []
    observed_wait = []
    future = []

    def redispatch():
        with factory() as db, db.begin():
            writer_pid.append(
                db.connection().execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            started.set()
            return dispatch_balances_due(
                db, redis_client, now=datetime.now(UTC) + timedelta(seconds=1)
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with factory() as observer, observer.begin():
            old = observer.get(
                ReportBalanceRun, UUID(payload["run_id"]), with_for_update=True
            )
            old.status = "FAILED"
            connection = observer.connection()

            def interleave(_conn, _cursor, sql, _params, _context, _many):
                if future or not sql.startswith("SELECT sync_schedule.scope_id"):
                    return
                future.append(executor.submit(redispatch))
                assert started.wait(3)
                deadline = monotonic() + 3
                with factory() as probe:
                    while monotonic() < deadline and not future[0].done():
                        waiting = (
                            probe.connection()
                            .execute(
                                text(
                                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"
                                ),
                                {"pid": writer_pid[0]},
                            )
                            .scalar_one()
                        )
                        if waiting == "Lock":
                            observed_wait.append(True)
                            break
                        sleep(0.01)

            event.listen(connection, "after_cursor_execute", interleave)
            try:
                balance_outcome(observer, old)
                assert observed_wait, (
                    "new dispatch was not fenced by outcome's head lock"
                )
            finally:
                event.remove(connection, "after_cursor_execute", interleave)
        assert future[0].result(timeout=5) == 1
    with factory() as db:
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        assert head.lease_execution_id == newer_id and head.lease_token is not None


def test_successful_page_does_not_reset_run_retry_budget(
    monkeypatch, committed_sync_seed, redis_client
):
    from decimal import Decimal

    import app.modules.sync_control.balance_demand as demand
    from app.integrations.tiktok.contracts.ads import AccountBalance, CallEvidence
    from tests.modules.reporting.test_balance_refresh import claim_balance, create_run

    seed, factory = committed_sync_seed
    clock = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(balance_tasks, "datetime", Clock)
    monkeypatch.setattr(demand, "datetime", Clock)
    run_id = create_run(seed, factory, ["acc-1", "acc-2"])
    payload = claim_balance(seed, factory, redis_client, run_id)
    calls = []

    @contextmanager
    def gateway(**_kwargs):
        def read(advertisers, *, page):
            calls.append(page)
            if len(calls) != 2:
                raise DomainError(
                    "tiktok_response_error", "provider intermittent error"
                )
            assert advertisers == ("acc-1", "acc-2") and page == 1
            return {
                "acc-1": AccountBalance(
                    Decimal("1"),
                    "USD",
                    "AVAILABLE",
                    clock[0],
                    CallEvidence(),
                    "ADVERTISER",
                    "acc-1",
                )
            }, 2

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert invoke(seed, payload) == "WAITING"
    with factory() as db:
        clock[0] = db.get(ReportBalanceRun, run_id).next_attempt_at + timedelta(
            seconds=1
        )
    payload = create_claim(seed, factory, redis_client, run_id=run_id, now=clock[0])
    assert invoke(seed, payload) == "CONTINUE"
    payload = create_claim(seed, factory, redis_client, run_id=run_id, now=clock[0])
    assert invoke(seed, payload) == "WAITING"
    with factory() as db:
        run = db.get(ReportBalanceRun, run_id)
        assert run.provider_failures == 2 and run.next_page == 2
        assert run.remaining_advertiser_ids == ["acc-2"]
        children = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.balance_run_id == run_id
            )
        ).all()
        assert {child.status for child in children} == {"PUBLISHED", "PENDING"}
    assert calls == [1, 1, 2]


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize(
    "granted,available", [(False, False), (True, False), (True, True)]
)
def test_real_adapter_missing_balance_does_not_satisfy_requirement(
    database_engine, redis_client, gateway_case, gateway_wire, granted, available
):
    from sqlalchemy import delete
    from sqlmodel import Session

    from app.modules.reporting.models import AccountBalanceObservation
    from app.modules.sync_control.models import SyncRequirement
    from tests.integrations.tiktok.test_ads_read_adapters import (
        authenticated_subject,
        page,
        replies,
    )

    context, route, advertiser = gateway_case
    authenticated_subject(database_engine, gateway_case)
    member = {
        "user_id": "subject-1",
        "relation_status": "BOUND",
        "ext_user_role": {"finance_role": "ANALYST" if granted else None},
    }
    rows = (
        [
            {
                "advertiser_id": advertiser,
                "currency": "USD",
                "balance_info": {"account_balance": "0"},
            }
        ]
        if available
        else []
    )
    replies(
        gateway_wire,
        "OFFICIAL_API",
        {
            "bc_member_get": [page([member], size=20)],
            "advertiser_balance_get": [
                {
                    "advertiser_account_list": rows,
                    "page_info": page(rows, size=100)["page_info"],
                }
            ],
        },
    )
    run_id = None
    try:
        with Session(database_engine) as db, db.begin():
            run = ReportBalanceRun(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                run_key=uuid4().hex,
                frozen_route=route.model_dump(mode="json"),
                remaining_advertiser_ids=[advertiser],
            )
            db.add(run)
            db.flush()
            run_id = run.id
            register_balance(db, run)
            assert (
                dispatch_balances_due(
                    db, redis_client, now=datetime.now(UTC) + timedelta(seconds=1)
                )
                == 1
            )
            payload = dict(
                db.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == context.tenant_id,
                        PendingDispatch.task_name == balance_tasks.TASK_NAME,
                    )
                )
                .one()
                .payload
            )
        from app.modules.sync_control.dispatch_envelope import dispatch_envelope

        with Session(database_engine) as db:
            message = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    PendingDispatch.payload == payload,
                )
            ).one()
            envelope = dispatch_envelope(message)
        result = balance_tasks.balance_step.run(
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            payload=payload,
            dispatch=envelope,
        )
        with Session(database_engine) as db:
            observation = db.exec(
                select(AccountBalanceObservation).where(
                    AccountBalanceObservation.tenant_id == context.tenant_id
                )
            ).one()
            current = db.get(ReportBalanceRun, run_id)
            child = db.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.balance_run_id == run_id
                )
            ).one()
            requirement = db.get(SyncRequirement, child.requirement_id)
            assert current.provider_failures == 0
            if available:
                assert (
                    observation.amount == 0 and observation.availability == "AVAILABLE"
                )
                assert result == "COMPLETE" and current.status == "COMPLETE"
                assert (
                    current.remaining_advertiser_ids == []
                    and child.status == "PUBLISHED"
                )
                assert requirement.satisfied_revision == requirement.desired_revision
            else:
                assert observation.amount is None
                assert observation.availability == (
                    "BALANCE_MISSING" if granted else "PERMISSION_UNVERIFIED"
                )
                assert result == "WAITING"
                assert (
                    current.status == "ACTIVE"
                    and current.remaining_advertiser_ids == [advertiser]
                )
                assert current.next_attempt_at > datetime.now(UTC)
                assert child.status == "PENDING" and child.observed_at is None
                assert requirement.satisfied_revision < requirement.desired_revision
    finally:
        with Session(database_engine) as db, db.begin():
            db.exec(
                delete(AccountBalanceObservation).where(
                    AccountBalanceObservation.tenant_id == context.tenant_id
                )
            )
            if run_id is not None:
                db.exec(
                    delete(SyncExecutionScope).where(
                        SyncExecutionScope.balance_run_id == run_id
                    )
                )
                db.exec(delete(ReportBalanceRun).where(ReportBalanceRun.id == run_id))


def test_planner_and_worker_lock_reverse_uuid_scopes_without_deadlock(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import monotonic, sleep

    from sqlalchemy import event, text

    from app.modules.sync_control.balance_demand import balance_fence, balance_scopes

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        scopes = balance_scopes(
            db,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            route=_route(seed),
            advertisers=["acc-1", "acc-2"],
        )
        base = uuid4().int & ~3
        scopes["acc-1"].id, scopes["acc-2"].id = UUID(int=base + 2), UUID(int=base + 1)
        db.flush()
        run = ReportBalanceRun(
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            run_key=uuid4().hex,
            frozen_route=_route(seed),
            remaining_advertiser_ids=["acc-1", "acc-2"],
        )
        db.add(run)
        db.flush()
        register_balance(db, run, scopes=scopes)
        run_id = run.id
    barrier = Barrier(2)
    worker_pid = []
    held_first = []

    def worker():
        with factory() as db, db.begin():
            worker_pid.append(
                db.connection().execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            current = db.get(ReportBalanceRun, run_id, with_for_update=True)
            barrier.wait(timeout=5)
            balance_fence(db, current)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        with factory() as planner_db, planner_db.begin():
            connection = planner_db.connection()

            def after_first(_conn, _cursor, sql, _params, _context, _many):
                if (
                    held_first
                    or not sql.startswith("SELECT sync_scope.")
                    or "FOR UPDATE" not in sql
                ):
                    return
                held_first.append(True)
                barrier.wait(timeout=5)
                deadline = monotonic() + 5
                with factory() as probe:
                    while monotonic() < deadline:
                        wait = (
                            probe.connection()
                            .execute(
                                text(
                                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"
                                ),
                                {"pid": worker_pid[0]},
                            )
                            .scalar_one()
                        )
                        probe.commit()
                        if wait == "Lock":
                            return
                        sleep(0.01)
                pytest.fail("worker did not reach the first account lock")

            event.listen(connection, "after_cursor_execute", after_first)
            try:
                locked = balance_scopes(
                    planner_db,
                    tenant_id=seed.tenant_id,
                    actor_id=seed.actor_id,
                    route=_route(seed),
                    advertisers=["acc-2", "acc-1"],
                )
                assert locked["acc-1"].id > locked["acc-2"].id
            finally:
                event.remove(connection, "after_cursor_execute", after_first)
        future.result(timeout=10)
    assert held_first


@pytest.mark.parametrize("error_kind", ["validation", "deadlock"])
def test_before_http_local_error_does_not_consume_provider_budget(
    monkeypatch, committed_sync_seed, redis_client, error_kind
):
    import psycopg
    from sqlalchemy.exc import OperationalError

    seed, factory = committed_sync_seed
    payload = create_claim(seed, factory, redis_client)

    def local_failure(*_args, **_kwargs):
        if error_kind == "deadlock":
            raise OperationalError(
                "local claim", {}, psycopg.errors.DeadlockDetected("local DB lock")
            )
        raise ValueError("local frozen route validation")

    monkeypatch.setattr(balance_tasks, "verify_read_routes", local_failure)
    monkeypatch.setattr(
        balance_tasks,
        "open_tiktok_gateway",
        lambda **_: pytest.fail("local failure opened HTTP"),
    )
    invoke(seed, payload)
    with factory() as db:
        run = db.get(ReportBalanceRun, UUID(payload["run_id"]))
        assert run.provider_failures == 0
