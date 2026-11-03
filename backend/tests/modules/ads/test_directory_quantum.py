"""背景大目录不能每读一页就重新等待整个账户池。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads import tasks
from app.modules.ads.sync_models import AdDirectoryPage
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule
from tests.modules.ads.test_directory_sync import (
    _authority,
    _claim_directory,
    _page,
    _run,
)
from tests.modules.reporting.sync_conftest import committed_sync_seed  # noqa: F401


@pytest.mark.parametrize("workload,want", [("background", 5), ("live", 1)])
def test_quantum_commits_each_page_and_yields_at_bound(
    session, directory_seed, redis_client, monkeypatch, workload, want
):
    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    run.workload = workload
    session.flush()
    generation = _claim_directory(session, run, redis_client, monkeypatch)
    calls, leases, deadlines = [], [], []

    class Reader:
        def read_page(self, query):
            # 下一次远端读取前，前页必须已经持久化；仍是同一领取及期限。
            if calls:
                assert session.get(AdDirectoryPage, (run.id, calls[-1])) is not None
            session.refresh(run)
            leases.append((run.claim_token, run.claimed_until))
            calls.append(query.page)
            return _page(
                directory_seed,
                page=query.page,
                complete=False,
                next_page=query.page + 1,
            )

    @contextmanager
    def gateway(**kwargs):
        deadlines.append(kwargs["task_deadline"])
        yield SimpleNamespace(ads=Reader())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    args = {
        "database_engine": session.connection(),
        "redis_client": redis_client,
        "context": directory_seed.context,
        "run_id": run.id,
        "claim_generation": generation,
    }
    assert tasks.run_directory_step(**args) == "CONTINUE"
    session.refresh(run)
    assert calls == list(range(1, want + 1))
    assert len(set(leases)) == len(set(deadlines)) == 1
    assert run.next_page == want + 1 and run.claimed_until is None
    assert run.claim_generation == generation
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == run.tenant_id)
    ).one()
    assert head.active and head.lease_token is None
    ledger = session.exec(
        select(SyncCostLedger).where(SyncCostLedger.directory_run_id == run.id)
    ).one()
    assert ledger.reserved_units == want
    # 已交回后旧消息不能越过规范领取再次读下一页。
    assert tasks.run_directory_step(**args) == "WAIT"
    assert len(calls) == want


def test_quantum_terminal_second_page_hands_off_without_extra_read(
    session, directory_seed, redis_client, monkeypatch
):
    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    run.workload = "background"
    session.flush()
    generation = _claim_directory(session, run, redis_client, monkeypatch)
    calls = []

    class Reader:
        def read_page(self, query):
            calls.append(query.page)
            return _page(
                directory_seed,
                page=query.page,
                complete=query.page == 2,
                next_page=2 if query.page == 1 else None,
            )

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=Reader())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    args = {
        "database_engine": session.connection(),
        "redis_client": redis_client,
        "context": directory_seed.context,
        "run_id": run.id,
        "claim_generation": generation,
    }
    assert tasks.run_directory_step(**args) == "CONTINUE"
    assert calls == [1, 2]
    session.refresh(run)
    assert run.next_page == 3 and run.claimed_until is None
    assert session.get(AdDirectoryPage, (run.id, 2)).complete
    assert tasks.run_directory_step(**args, publish_only=True) == "READY"
    session.refresh(run)
    assert run.status == "COMPLETE"


@pytest.fixture
def background_case(session, directory_seed, redis_client, monkeypatch):
    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run = _run(session, directory_seed)
    run.workload = "background"
    session.flush()
    generation = _claim_directory(session, run, redis_client, monkeypatch)
    args = {
        "database_engine": session.connection(),
        "redis_client": redis_client,
        "context": directory_seed.context,
        "run_id": run.id,
        "claim_generation": generation,
    }
    return run, args


def _reader(monkeypatch, read):
    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)


def test_duplicate_between_pages_cannot_read_or_steal_claim(
    session, directory_seed, background_case, monkeypatch
):
    run, args = background_case
    seen = []

    def read(query):
        if query.page == 2:
            session.refresh(run)
            before = (run.claim_token, run.claimed_until)
            assert tasks.run_directory_step(**args) == "WAIT"
            session.refresh(run)
            assert (run.claim_token, run.claimed_until) == before
        seen.append(query.page)
        return _page(
            directory_seed,
            page=query.page,
            complete=query.page == 3,
            next_page=None if query.page == 3 else query.page + 1,
        )

    _reader(monkeypatch, read)
    assert tasks.run_directory_step(**args) == "CONTINUE"
    assert seen == [1, 2, 3]


def test_third_page_admission_preserves_two_committed_pages_and_due(
    session, directory_seed, background_case, monkeypatch
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    run, args = background_case
    seen = []

    def read(query):
        seen.append(query.page)
        if query.page == 3:
            raise AccountAdmissionDeferred(5000)
        return _page(
            directory_seed, page=query.page, complete=False, next_page=query.page + 1
        )

    _reader(monkeypatch, read)
    before = datetime.now(UTC)
    assert tasks.run_directory_step(**args) == "WAIT"
    session.refresh(run)
    assert seen == [1, 2, 3]
    assert run.next_page == 3 and run.claim_generation == args["claim_generation"] + 1
    assert run.claim_token is None and run.claimed_until is None
    assert run.next_attempt_at >= before + timedelta(seconds=5)
    assert session.get(AdDirectoryPage, (run.id, 1)) is not None
    assert session.get(AdDirectoryPage, (run.id, 2)) is not None
    assert session.get(AdDirectoryPage, (run.id, 3)) is None


@pytest.mark.parametrize("change", ["generation", "owner", "head"])
def test_quantum_rechecks_fences_before_later_http(
    session, directory_seed, background_case, monkeypatch, change
):
    from app.modules.sync_control.models import SyncScope

    run, args = background_case
    seen = []
    # 在第二页即将领取的真实事务边界模拟前一页之后的所有权变化。
    original = tasks._directory_session
    calls = 0
    later_head_token = uuid4()

    @contextmanager
    def directory_session(*a, **kw):
        nonlocal calls
        calls += 1
        if calls == 3:
            session.refresh(run)
            if change == "generation":
                run.claim_generation += 1
                run.claim_token = uuid4()
                run.claimed_until = datetime.now(UTC) + timedelta(minutes=2)
            elif change == "owner":
                scope = session.exec(
                    select(SyncScope).where(SyncScope.tenant_id == run.tenant_id)
                ).one()
                scope.owner_epoch += 1
            elif change == "head":
                head = session.exec(
                    select(SyncSchedule).where(SyncSchedule.tenant_id == run.tenant_id)
                ).one()
                head.lease_token = later_head_token
            session.flush()
        with original(*a, **kw) as db:
            yield db

    def read(query):
        seen.append(query.page)
        return _page(
            directory_seed, page=query.page, complete=False, next_page=query.page + 1
        )

    _reader(monkeypatch, read)
    monkeypatch.setattr(tasks, "_directory_session", directory_session)
    assert (
        tasks.run_directory_step(**args)
        == {"generation": "FAILED", "head": "WAIT", "owner": "WAIT"}[change]
    )
    assert seen == [1]
    session.refresh(run)
    assert run.next_page == 2
    assert session.get(AdDirectoryPage, (run.id, 1)) is not None
    if change == "generation":
        assert run.claim_generation == args["claim_generation"] + 1
        assert run.claim_token is not None
    elif change == "head":
        assert run.claim_token is None
        head = session.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == run.tenant_id)
        ).one()
        assert head.lease_token == later_head_token  # 保留后来签发的同ID/代数grant。


def test_quantum_near_deadline_commits_page_and_yields_without_more_http(
    session, directory_seed, background_case, monkeypatch
):
    run, args = background_case
    real_datetime = datetime
    offset = timedelta()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + offset

    seen = []

    def read(query):
        nonlocal offset
        seen.append(query.page)
        offset = timedelta(seconds=43)
        return _page(
            directory_seed, page=query.page, complete=False, next_page=query.page + 1
        )

    monkeypatch.setattr(tasks, "datetime", Clock)
    _reader(monkeypatch, read)
    assert tasks.run_directory_step(**args) == "CONTINUE"
    assert seen == [1]
    session.refresh(run)
    assert run.next_page == 2 and run.claimed_until is None


def test_interrupted_third_page_retains_committed_cursor_and_unknown_cost(
    session, directory_seed, background_case, monkeypatch, redis_client
):
    from billiard.exceptions import SoftTimeLimitExceeded

    run, args = background_case
    seen = []

    def read(query):
        seen.append(query.page)
        if query.page == 3:
            raise SoftTimeLimitExceeded()
        return _page(
            directory_seed, page=query.page, complete=False, next_page=query.page + 1
        )

    _reader(monkeypatch, read)
    with pytest.raises(SoftTimeLimitExceeded):
        tasks.run_directory_step(**args)
    session.refresh(run)
    assert seen == [1, 2, 3] and run.next_page == 3
    assert session.get(AdDirectoryPage, (run.id, 2)) is not None
    assert session.get(AdDirectoryPage, (run.id, 3)) is None
    old_ledger = session.exec(
        select(SyncCostLedger).where(
            SyncCostLedger.directory_run_id == run.id,
            SyncCostLedger.generation == args["claim_generation"],
        )
    ).one()
    assert not old_ledger.closed and old_ledger.charged_units == 5
    # 模拟原短租约自然过期，再经原规范调度领取；从第三页继续，不重读前两页。
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == run.tenant_id)
    ).one()
    run.claimed_until = head.leased_until = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    generation = _claim_directory(session, run, redis_client, monkeypatch)
    args["claim_generation"] = generation

    def resumed(query):
        seen.append(query.page)
        return _page(
            directory_seed,
            page=query.page,
            complete=query.page == 4,
            next_page=None if query.page == 4 else query.page + 1,
        )

    _reader(monkeypatch, resumed)
    assert tasks.run_directory_step(**args) == "CONTINUE"
    assert seen == [1, 2, 3, 3, 4]
    session.refresh(run)
    assert run.next_page == 5


@pytest.fixture
def committed_background(committed_sync_seed, redis_client, monkeypatch):  # noqa: F811
    from app.core.context import TenantContext
    from tests.modules.sync_control.test_schedule_fencing import _directory

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    with factory() as db:
        run = _directory(
            db,
            seed,
            query_extra={
                "ids": [],
                "advertiser_id": "acc-1",
                "page_size": 100,
                "include_deleted": False,
            },
        )
        run.workload = "background"
        db.flush()
        generation = _claim_directory(db, run, redis_client, monkeypatch)
        identity, tenant = run.id, run.tenant_id
        db.commit()
        database_engine = db.get_bind()
    return (
        seed,
        factory,
        {
            "database_engine": database_engine,
            "redis_client": redis_client,
            "context": TenantContext(
                tenant_id=tenant, actor_id=seed.actor_id, role="operator"
            ),
            "run_id": identity,
            "claim_generation": generation,
        },
    )


def test_quantum_revocation_between_committed_pages_stops_next_http(
    committed_background, monkeypatch
):
    from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory, args = committed_background
    identity, tenant = args["run_id"], seed.tenant_id
    original = tasks._directory_session
    calls, seen = 0, []

    @contextmanager
    def directory_session(*a, **kw):
        nonlocal calls
        calls += 1
        if calls == 3:
            # 第一页已实际COMMIT，另一连接撤销绑定。
            with factory() as control, control.begin():
                assert control.get(AdDirectoryPage, (identity, 1)) is not None
                binding = control.get(
                    BCConnectionBinding, (tenant, "bc-a", seed.connection.id)
                )
                binding.status = "DISABLED"
        with original(*a, **kw) as db:
            yield db

    def read(query):
        seen.append(query.page)
        return DirectoryPage(
            items=(),
            materials=(),
            page=query.page,
            complete=False,
            next_page=query.page + 1,
            evidence=CallEvidence(request_id="quantum-local"),
        )

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_primary_page=read))

    monkeypatch.setattr(tasks, "_directory_session", directory_session)
    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    assert tasks.run_directory_step(**args) == "FAILED"
    assert seen == [1]
    with factory() as db:
        current = db.get(AdDirectoryRun, identity)
        assert (
            current.status == "FAILED"
            and current.error_code == "connection_bc_mismatch"
        )
        assert current.next_page == 2 and current.claimed_until is None
        assert db.get(AdDirectoryPage, (identity, 1)) is not None
        assert db.get(AdDirectoryPage, (identity, 2)) is None


def test_quantum_transport_receipts_include_retries_auxiliary_and_early_refund(
    committed_background, monkeypatch
):
    from app.integrations.tiktok.attempt_observer import physical_attempt_scope
    from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
    from app.integrations.tiktok.transport_diagnostics import install_transport_tracking
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.costs import settle_idle_costs
    from app.modules.sync_control.directory_demand import directory_endpoint

    seed, factory, args = committed_background
    with factory() as db:
        endpoint = directory_endpoint(db.get(AdDirectoryRun, args["run_id"]))

    class Connection:
        def request(self):
            return "local-response"

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
    seen, leases = [], []

    def read(query):
        with factory() as observer:
            current = observer.get(AdDirectoryRun, args["run_id"])
            leases.append((current.claim_token, current.claimed_until))
            if query.page == 2:
                assert observer.get(AdDirectoryPage, (current.id, 1)) is not None
        seen.append(query.page)
        with physical_attempt_scope(client, endpoint):
            # 第一页一次重试，第二页一次读取；不能只按页数记成本。
            connection.request()
            if query.page == 1:
                connection.request()
        if query.page == 1:
            with physical_attempt_scope(client, "GET /independent/material/get/"):
                connection.request()
        return DirectoryPage(
            items=(),
            materials=(),
            page=query.page,
            complete=query.page == 2,
            next_page=2 if query.page == 1 else None,
            evidence=CallEvidence(request_id="quantum-local"),
        )

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_primary_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    assert tasks.run_directory_step(**args) == "CONTINUE"
    assert seen == [1, 2] and len(set(leases)) == 1
    assert settle_idle_costs(args["database_engine"], budget_seconds=2) == 2
    with factory() as db:
        rows = db.exec(
            select(SyncCostLedger).where(
                SyncCostLedger.directory_run_id == args["run_id"]
            )
        ).all()
        assert {
            r.physical_endpoint: (r.reserved_units, r.observed_units, r.charged_units)
            for r in rows
        } == {endpoint: (5, 3, 3), "GET /independent/material/get/": (1, 1, 1)}
        assert all(r.closed and len(r.producers) == 1 for r in rows)


def test_terminal_page_survives_head_expiry_and_canonical_publication_recovery(
    session, directory_seed, background_case, monkeypatch, redis_client
):
    run, args = background_case
    seen = []

    def read(query):
        seen.append(query.page)
        if query.page == 2:
            head = session.exec(
                select(SyncSchedule).where(SyncSchedule.tenant_id == run.tenant_id)
            ).one()
            head.leased_until = datetime.now(UTC) - timedelta(seconds=1)
            session.flush()
        return _page(
            directory_seed,
            page=query.page,
            complete=query.page == 2,
            next_page=2 if query.page == 1 else None,
        )

    _reader(monkeypatch, read)
    assert tasks.run_directory_step(**args) == "CONTINUE"
    session.refresh(run)
    assert seen == [1, 2] and run.next_page == 3
    assert session.get(AdDirectoryPage, (run.id, 2)).complete
    args["claim_generation"] = _claim_directory(session, run, redis_client, monkeypatch)
    assert tasks.run_directory_step(**args, publish_only=True) == "READY"
    assert seen == [1, 2]
    session.refresh(run)
    assert run.status == "COMPLETE"


def test_quantum_local_transaction_uses_remaining_absolute_budget():
    from time import monotonic

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.core.db import engine

    started = monotonic()
    with pytest.raises(DBAPIError, match="transaction timeout"):
        with tasks._directory_session(
            engine, deadline=datetime.now(UTC) + timedelta(milliseconds=180)
        ) as db:
            timeout = db.execute(text("SHOW transaction_timeout")).scalar_one()
            assert timeout.endswith("ms") and 0 < int(timeout[:-2]) <= 180
            for _ in range(16):
                db.execute(text("SELECT pg_sleep(0.06)"))
    assert monotonic() - started < 2


def test_nonterminal_head_expiry_releases_own_claim_without_second_http(
    session, directory_seed, background_case, monkeypatch, redis_client
):
    from app.modules.sync_control.worker_pool import background_lease_count

    run, args = background_case
    seen = []

    def read(query):
        seen.append(query.page)
        head = session.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == run.tenant_id)
        ).one()
        head.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()
        return _page(
            directory_seed, page=query.page, complete=False, next_page=query.page + 1
        )

    _reader(monkeypatch, read)
    assert tasks.run_directory_step(**args) == "WAIT"
    session.refresh(run)
    assert seen == [1] and run.next_page == 2
    assert run.claim_token is None and run.claimed_until is None
    assert background_lease_count(session, now=datetime.now(UTC)) == 0
    assert (
        _claim_directory(session, run, redis_client, monkeypatch)
        == args["claim_generation"] + 1
    )
