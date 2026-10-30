"""实时续点复用剩余预算，失败只回滚当前页/批。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.core.db import engine
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting import job_tasks, planner
from app.modules.reporting import planning_steps as steps
from app.modules.reporting.store_models import AdSyncBc
from tests.modules.reporting.test_job_tasks import _ctx
from tests.modules.reporting.test_planning_opportunities import _prepared


@pytest.fixture
def planning_keys(monkeypatch, redis_client):
    prefix = f"test:planner:{uuid4()}"
    keys = (prefix + ":entry", prefix + ":continuation")
    monkeypatch.setattr(job_tasks, "PLANNING_ENTRY_LOCK", keys[0], raising=False)
    monkeypatch.setattr(job_tasks, "PLANNING_CONTINUATION", keys[1], raising=False)
    yield keys
    redis_client.delete(*keys)


def test_entry_mutex_skips_work_and_expired_owner_cannot_delete_successor(
    monkeypatch, committed_sync_seed, redis_client, planning_keys
):
    _seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    redis_client.set(planning_keys[0], "other-worker", ex=35)
    assert (
        job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client) == 0
    )
    with factory() as session:
        from sqlmodel import select

        assert not session.exec(select(AdSyncBc)).all()
    assert redis_client.get(planning_keys[0]) == "other-worker"


def test_entry_releases_lease_on_failure_and_following_tick_recovers(
    monkeypatch, redis_client, planning_keys
):
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)

    def failed(*_a, **_kw):
        # 真Redis锁已取得；异常后的finally必须释放自己的租约。
        assert redis_client.get(planning_keys[0])
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(planner, "ensure_bc_plans", failed)
    with pytest.raises(RuntimeError, match="prepare failed"):
        job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client)
    assert not redis_client.exists(planning_keys[0])


def test_screen_uses_remaining_budget_for_a_second_committed_page(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "screen_hot")
    seen = []

    class Reader:
        def bc_spend(self, **kw):
            seen.append(kw["page"])
            return RawReportPage((), kw["page"], 3, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))
    for budget in (7, 4.5):
        # 每次在真实事务中提交一页，再模拟跨BC轮转后回到该screen槽。
        with factory() as session, session.begin():
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            state = dict(plan.screening_progress[steps.KEY])
            state["phase"] = steps.PHASE_ORDER.index("screen_hot")
            plan.screening_progress = {**plan.screening_progress, steps.KEY: state}
        steps.plan_step(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now=now,
            allow_backfill=False,
            budget=budget,
        )
    assert seen == [1, 2]
    with factory() as session:
        assert (
            session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress["bc:1"][
                "page"
            ]
            == 3
        )


@pytest.mark.parametrize("sqlstate", ["40P01", "55P03"])
def test_lock_conflict_rolls_back_only_phase_without_shrinking_batch(
    monkeypatch, committed_sync_seed, redis_client, sqlstate
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "hot")

    def conflict(session, *_):
        session.execute(
            text(
                "DO $$ BEGIN RAISE EXCEPTION 'test lock conflict' USING ERRCODE = '"
                + sqlstate
                + "'; END $$"
            )
        )

    monkeypatch.setattr(steps, "_execute", conflict)
    metrics = {}
    steps.plan_step(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now=now,
        allow_backfill=False,
        budget=8,
        metrics=metrics,
    )
    with factory() as session:
        state = session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress[
            steps.KEY
        ]
        assert state["hot"]["size"] == 64
        assert state["hot"]["after"] == ""
        assert not state["hot"]["deferred"]
        assert datetime.fromisoformat(state["hot"]["retry_at"]) == now + timedelta(
            seconds=5
        )
        assert not state.get("retry_at")
    assert metrics.get("completed", 0) == 0
    assert metrics["deferred"] == 1


def test_history_planning_does_not_create_or_lock_fact_partitions(session, sync_seed):
    from sqlmodel import select

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.store_models import ReportSyncJob

    now = datetime(2039, 5, 1, tzinfo=UTC)
    planner.ensure_bc_plans(session, now=now)
    session.get(
        AdvertiserAccount, (sync_seed.tenant_id, "acc-1")
    ).remote_created_at = datetime(2039, 1, 1, tzinfo=UTC)
    session.flush()
    statements = []

    def capture(_conn, _cursor, statement, *_):
        if statement.startswith(("LOCK TABLE", "CREATE TABLE", "ALTER TABLE")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
        assert (
            planner.plan_history(
                session, plan, now=now, accounts=["acc-1"], progress={}
            )
            > 0
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not statements
    assert session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()


def test_continuation_is_coalesced_durable_realtime_only_and_not_recursive(
    monkeypatch, committed_sync_seed, redis_client, planning_keys
):
    from sqlmodel import select

    from app.jobs.models import PendingDispatch
    from app.modules.reporting import link_work

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "directory")
    # 两个普通轮的信号合并到同一个持久outbox；模拟后半轮尚有实时工作。
    job_tasks._schedule_planning_continuation(engine, redis_client, now, now)
    job_tasks._schedule_planning_continuation(engine, redis_client, now, now)
    with factory() as session:
        rows = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "reporting.plan_continue",
                PendingDispatch.tenant_id == seed.tenant_id,
            )
        ).all()
        assert len(rows) == 1
        row = rows[0]
        payload = row.payload
        assert row.available_at == now + timedelta(seconds=15)
        assert datetime.fromisoformat(payload["expires_at"]) == now + timedelta(
            seconds=30
        )
    assert job_tasks.celery_app.conf.task_routes["reporting.plan_continue"] == {
        "queue": "control"
    }

    def forbidden(*_a, **_kw):
        pytest.fail("续点不得运行整轮维护/目录扫描")

    monkeypatch.setattr(planner, "ensure_bc_plans", forbidden)
    monkeypatch.setattr(job_tasks, "_dispatch_directories", forbidden)
    monkeypatch.setattr(link_work, "wake_link_dependencies", forbidden)

    class Reader:
        def bc_spend(self, **_):
            return RawReportPage((), 1, 1, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))
    job_tasks.plan_due_runs(
        database_engine=engine, redis_client=redis_client, now=now, continuation=payload
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.screening_progress[steps.KEY]["screen_hot"]["done"]
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.task_name == "reporting.plan_continue",
                        PendingDispatch.tenant_id == seed.tenant_id,
                    )
                ).all()
            )
            == 1
        )
    assert not redis_client.exists(planning_keys[1])
    monkeypatch.setattr(job_tasks, "_run_planning_entry", forbidden)
    assert (
        job_tasks.plan_due_runs(
            database_engine=engine, redis_client=redis_client, continuation=payload
        )
        == 0
    )


def test_expired_continuation_and_old_lock_owner_cannot_remove_new_markers(
    monkeypatch, redis_client, planning_keys
):
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    identity = str(uuid4())
    redis_client.set(planning_keys[1], identity, ex=60)
    payload = {
        "continuation_id": identity,
        "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }
    assert (
        job_tasks.plan_due_runs(
            database_engine=engine, redis_client=redis_client, continuation=payload
        )
        == 0
    )
    assert not redis_client.exists(planning_keys[1])
    redis_client.set(planning_keys[0], "successor", ex=35)
    job_tasks._release_planning_key(redis_client, planning_keys[0], "expired-owner")
    assert redis_client.get(planning_keys[0]) == "successor"
    # 模拟进程退出后租约自然到期；普通轮无需依赖旧worker清理即可重新取得。
    redis_client.pexpire(planning_keys[0], 0)
    called = []
    monkeypatch.setattr(
        job_tasks, "_run_planning_entry", lambda **_: called.append(True) or 0
    )
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client)
    assert called == [True]


def test_late_continuation_does_not_hold_lock_across_next_regular_tick(
    monkeypatch, redis_client, planning_keys
):
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    identity = str(uuid4())
    redis_client.set(planning_keys[1], identity, ex=60)

    def forbidden(**_):
        pytest.fail("第25秒抵达的续点不能再占10秒跨过普通第30秒入口")

    monkeypatch.setattr(job_tasks, "_run_planning_entry", forbidden)
    payload = {
        "continuation_id": identity,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
    }
    assert (
        job_tasks.plan_due_runs(
            database_engine=engine, redis_client=redis_client, continuation=payload
        )
        == 0
    )
    assert not redis_client.exists(planning_keys[0], planning_keys[1])


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"continuation_id": "bad", "expires_at": "bad"},
        {"dispatch_only": True},
    ],
)
def test_continuation_task_rejects_non_contract_payload_before_opening_redis(
    monkeypatch, payload
):
    from app.core.errors import DomainError

    def forbidden(*_a, **_kw):
        pytest.fail("非法续点不能退化为普通轮或补位入口")

    monkeypatch.setattr(job_tasks.Redis, "from_url", forbidden)
    with pytest.raises(DomainError, match="续点|参数"):
        job_tasks.plan_continue(
            tenant_id=str(uuid4()), actor_id=str(uuid4()), payload=payload
        )


def test_outbox_failure_releases_coalescing_marker_and_next_attempt_recovers(
    monkeypatch, committed_sync_seed, redis_client, planning_keys
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "hot")
    enqueue = job_tasks.enqueue_sync_dispatch

    def fail(*_a, **_kw):
        raise RuntimeError("outbox transaction cancelled")

    monkeypatch.setattr(job_tasks, "enqueue_sync_dispatch", fail)
    with pytest.raises(RuntimeError, match="outbox transaction cancelled"):
        job_tasks._schedule_planning_continuation(engine, redis_client, now, now)
    assert not redis_client.exists(planning_keys[1])
    monkeypatch.setattr(job_tasks, "enqueue_sync_dispatch", enqueue)
    job_tasks._schedule_planning_continuation(engine, redis_client, now, now)
    assert redis_client.exists(planning_keys[1])


def test_real_pg_prepare_timeout_never_enters_stage(monkeypatch, redis_client):
    import time

    def slow(session, **_):
        session.execute(text("SELECT pg_sleep(2)"))
        return []

    def forbidden(*_a, **_kw):
        pytest.fail("prepare耗尽总预算后不能启动阶段")

    monkeypatch.setattr(planner, "due_plans", slow)
    monkeypatch.setattr(job_tasks, "_plan_one", forbidden)
    started = time.monotonic()
    assert (
        job_tasks._run_planning_entry(
            database_engine=engine,
            redis_client=redis_client,
            realtime_only=True,
            stop=started + 0.15,
        )
        == 0
    )
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("slow_call", [1, 2])
def test_real_pg_slow_dispatch_preserves_maintenance_facts_and_continuation(
    monkeypatch, committed_sync_seed, redis_client, planning_keys, slow_call
):
    import time

    from sqlmodel import select

    from app.jobs.models import PendingDispatch
    from app.modules.reporting import link_work
    from app.modules.reporting.store_models import ReportSyncJob

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "_dispatch_directories", lambda *_a, **_kw: 0)
    maintenance = []
    monkeypatch.setattr(
        link_work,
        "wake_link_dependencies",
        lambda *_a, **_kw: maintenance.append("wake"),
    )
    original_entry = job_tasks._run_planning_entry

    def bounded_entry(**kw):
        return original_entry(**{**kw, "stop": time.monotonic() + 4})

    monkeypatch.setattr(job_tasks, "_run_planning_entry", bounded_entry)
    with factory() as session, session.begin():
        plan = _prepared(session, seed, now, "hot")
        state = dict(plan.screening_progress[steps.KEY])
        state["screen_hot"] = {**state["screen_hot"], "done": True}
        plan.screening_progress = {steps.KEY: state}
        plan.last_active_advertiser_ids = ["acc-1", "acc-2"]
    monkeypatch.setattr(planner, "ensure_bc_plans", lambda *_a, **_kw: None)
    calls = []

    def slow_tail(session, *_a, **_kw):
        calls.append(True)
        if len(calls) == slow_call:
            session.execute(text("SELECT pg_sleep(10)"))
        return 0

    monkeypatch.setattr(job_tasks, "_dispatch_report_jobs", slow_tail)
    started = time.monotonic()
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    assert len(calls) == 2
    assert time.monotonic() - started < 5
    assert maintenance == ["wake"]
    with factory() as session:
        kinds = set(
            session.exec(
                select(ReportSyncJob.kind).where(
                    ReportSyncJob.tenant_id == seed.tenant_id
                )
            ).all()
        )
        assert {"ad_day", "material_day"} <= kinds
        signals = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.plan_continue",
            )
        ).all()
        assert len(signals) == 1
        assert (
            redis_client.get(planning_keys[1]) == signals[0].payload["continuation_id"]
        )


def test_real_pg_failure_defer_respects_remaining_entry_budget(
    monkeypatch, committed_sync_seed
):
    import time

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "hot")
    original_select = job_tasks.select
    monkeypatch.setattr(
        job_tasks,
        "select",
        lambda *args: original_select(*args).where(text("pg_sleep(2) IS NULL")),
    )
    started = time.monotonic()
    job_tasks._defer_failed_plan(
        engine, (seed.tenant_id, "bc-a"), now, stop=started + 0.3
    )
    assert time.monotonic() - started < 1
    with factory() as session:
        assert (
            session.get(AdSyncBc, (seed.tenant_id, "bc-a")).disabled_reason
            != "plan_failed"
        )


def test_real_pg_metadata_timeout_rolls_back_before_any_phase(
    monkeypatch, committed_sync_seed, redis_client
):
    from sqlalchemy.exc import DBAPIError

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "hot")

    def slow(session, _plan):
        session.execute(text("SELECT pg_sleep(2)"))

    monkeypatch.setattr(planner, "owned_account_timezones", slow)
    with pytest.raises(DBAPIError) as error:
        steps.plan_step(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now=now,
            allow_backfill=False,
            budget=0.15,
        )
    assert error.value.orig.sqlstate == "57014"
    with factory() as session:
        state = session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress[
            steps.KEY
        ]
        assert not state["hot"].get("covered")
        assert not state.get("retry_at")


def test_failed_second_screen_page_keeps_first_committed_page(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.errors import DomainError

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "screen_hot")
    seen = []

    class Reader:
        def bc_spend(self, **kw):
            seen.append(kw["page"])
            if len(seen) == 2:
                raise DomainError("read_deadline_exceeded", "test timeout")
            return RawReportPage((), kw["page"], 3, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))
    for budget in (7, 3, 7):
        with factory() as session, session.begin():
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            state = dict(plan.screening_progress[steps.KEY])
            state["phase"] = 0
            plan.screening_progress = {**plan.screening_progress, steps.KEY: state}
        steps.plan_step(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now=now,
            allow_backfill=False,
            budget=budget,
        )
    assert seen == [1, 2, 2]
    with factory() as session:
        p = session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress
        assert p["bc:1"]["page"] == 3
        assert p[steps.KEY]["screen_hot"]["size"] == 64


@pytest.mark.parametrize("fallback", [False, True])
def test_four_bc_capacity_with_real_pg_pages_facts_and_slow_directory(
    monkeypatch, committed_sync_seed, redis_client, planning_keys, fallback
):
    """1.3s BC页/0.35s事实批验证15min；5s fallback明确只验证有界持续前进。"""
    from collections import Counter

    from sqlalchemy import delete
    from sqlmodel import select

    from app.core.context import TenantContext
    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.jobs.models import PendingDispatch
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import AdvertiserAccount, TenantBC
    from app.modules.accounts.routing import verify_route
    from app.modules.reporting import link_work
    from app.modules.reporting.query_budget import QueryBudgetExceeded
    from app.modules.reporting.store_models import AdSyncAccount, ReportSyncJob

    seed, factory = committed_sync_seed
    now = datetime(2026, 10, 29, 20, tzinfo=UTC)
    pools = {
        bc: [f"{bc}-{i:05d}" for i in range(n)]
        for bc, n in zip(
            ("bc-a", "bc-b", "bc-c", "bc-d"), (4859, 872, 202, 969), strict=True
        )
    }
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        first = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        route = first.frozen_route
        session.execute(
            delete(AdSyncAccount).where(AdSyncAccount.tenant_id == seed.tenant_id)
        )
        for bc, accounts in pools.items():
            if bc in {"bc-c", "bc-d"}:
                session.add(TenantBC(tenant_id=seed.tenant_id, bc_id=bc))
                session.flush()
                session.add(
                    BCConnectionBinding(
                        tenant_id=seed.tenant_id,
                        bc_id=bc,
                        connection_id=seed.connection.id,
                        kind="OFFICIAL_API",
                        status="ACTIVE",
                        authorization_revision=route["authorization_revision"],
                        revision=route["binding_revision"],
                    )
                )
                session.add(
                    AdSyncBc(
                        tenant_id=seed.tenant_id,
                        bc_id=bc,
                        actor_id=seed.actor_id,
                        frozen_route={**route, "bc_id": bc},
                        next_hot_at=now,
                        next_material_at=now,
                        next_daily_at=now,
                        next_directory_at=now,
                    )
                )
            session.add_all(
                [
                    AdvertiserAccount(
                        tenant_id=seed.tenant_id,
                        advertiser_id=a,
                        timezone="Asia/Tokyo" if i % 2 else "America/Los_Angeles",
                        currency="USD",
                    )
                    for i, a in enumerate(accounts)
                ]
            )
            session.flush()
            session.add_all(
                [
                    AdSyncAccount(
                        tenant_id=seed.tenant_id, advertiser_id=a, owner_bc_id=bc
                    )
                    for a in accounts
                ]
            )
            session.flush()
            plan = session.get(AdSyncBc, (seed.tenant_id, bc))
            plan.last_active_advertiser_ids = accounts
            plan.next_daily_at = now + timedelta(days=1)
            plan.next_directory_at = now if bc == "bc-c" else now + timedelta(days=1)
            plan.bc_report_supported = not (fallback and bc == "bc-d")
            # 合成BC也必须具备真实绑定，避免容量实验把鉴权拒绝误当调度饥饿。
            session.flush()
            verify_route(
                session,
                context=TenantContext(seed.tenant_id, seed.actor_id, "operator"),
                route=FrozenTikTokRoute.model_validate(plan.frozen_route),
                advertiser_id=None,
                capability="read",
            )

    clock = [0.0]

    class EntryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = now + timedelta(seconds=clock[0])
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    # outbox +15/+30和迟到准入必须与容量时钟一致，不能用真实墙钟立即消费模拟未来消息。
    monkeypatch.setattr(job_tasks, "datetime", EntryClock)
    reads = Counter()
    stages = Counter()
    background_modes = Counter()
    current_mode = ["regular"]
    page_budget = [8.0]
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(steps, "monotonic", lambda: clock[0])

    def consume(seconds):
        clock[0] += seconds

    monkeypatch.setattr(planner, "ensure_bc_plans", lambda *_a, **_kw: consume(2.0))
    monkeypatch.setattr(
        link_work, "wake_link_dependencies", lambda *_a, **_kw: consume(0.6)
    )
    dispatch_costs = []
    dispatch_call = [0]

    def timed_dispatch(*_a, **kw):
        # 两次派发都计成本；首片最多1s，尾片模拟1.2s慢补位且遵守真实余量。
        dispatch_call[0] += 1
        desired = 0.4 if dispatch_call[0] % 2 else 1.2
        spent = min(desired, kw["budget_seconds"])
        dispatch_costs.append(spent)
        consume(spent)
        if spent < desired:
            raise QueryBudgetExceeded()
        return 0

    monkeypatch.setattr(job_tasks, "_dispatch_pending", timed_dispatch)
    original = steps._execute

    def timed(session, plan, state, phase, *args):
        stages[(plan.bc_id, phase)] += 1
        if phase not in steps.REALTIME_PHASES:
            background_modes[current_mode[0]] += 1
        if phase == "directory":
            consume(min(3, args[-1]))
            raise QueryBudgetExceeded()
        result = original(session, plan, state, phase, *args)
        if not phase.startswith("screen_"):
            consume(0.35 if phase in {"hot", "material"} else 0.15)
        return result

    monkeypatch.setattr(steps, "_execute", timed)

    class Reader:
        def _page(self, accounts, page, total, key, seconds):
            if page_budget[0] < seconds:
                consume(page_budget[0])
                raise DomainError("read_deadline_exceeded", "test SDK deadline")
            consume(seconds)
            reads[key] += 1
            return RawReportPage(
                tuple(
                    {"dimensions": {"advertiser_id": a}, "metrics": {"spend": "1"}}
                    for a in accounts
                ),
                page,
                total,
                len(accounts),
            )

        def bc_spend(self, *, bc_id, page, **_):
            accounts = pools[bc_id]
            return self._page(
                accounts[(page - 1) * 1000 : page * 1000],
                page,
                (len(accounts) + 999) // 1000,
                bc_id,
                1.3,
            )

        def account_day(self, *, advertiser_ids, page, **_):
            return self._page(advertiser_ids, page, 1, "fallback", 5)

    def reader_for(**kw):
        page_budget[0] = (kw["deadline"] - datetime.now(UTC)).total_seconds()
        return _ctx(Reader())

    monkeypatch.setattr(job_tasks, "_reader_for", reader_for)
    completion = {}
    turns = Counter()
    fallback_offsets = []
    executed_signals = set()
    maximum_ticks = 8 if fallback else 30
    for tick in range(maximum_ticks):
        for extra in (False, True):
            slot = tick * 30 + (15 if extra else 0)
            clock[0] = slot
            payload = None
            if extra:
                identity = redis_client.get(planning_keys[1])
                if not identity:
                    continue
                with factory() as session:
                    row = session.exec(
                        select(PendingDispatch).where(
                            PendingDispatch.tenant_id == seed.tenant_id,
                            PendingDispatch.task_key
                            == f"report-planning-continue:{identity}",
                        )
                    ).one()
                    payload = row.payload
                    arrived_at = now + timedelta(seconds=slot)
                    assert (
                        row.available_at
                        <= arrived_at
                        < datetime.fromisoformat(payload["expires_at"])
                    )
                    assert identity not in executed_signals
                    executed_signals.add(identity)
            dispatch_call[0] = 0
            current_mode[0] = "continuation" if extra else "regular"
            turns["continuation" if extra else "regular"] += 1
            job_tasks.plan_due_runs(
                database_engine=engine,
                redis_client=redis_client,
                now=now + timedelta(seconds=slot),
                continuation=payload,
            )
            assert clock[0] - slot <= 10.001
            with factory() as session:
                for bc in pools:
                    p = session.get(AdSyncBc, (seed.tenant_id, bc)).screening_progress
                    state = p.get(steps.KEY)
                    if state is None:
                        continue  # 本入口预算耗尽前尚未轮到的BC仍保留下一轮机会。
                    if (
                        state["hot"].get("done")
                        and state["material"].get("done")
                        and state["screen_hot"].get("done")
                    ):
                        completion.setdefault(bc, clock[0])
                    if fallback and bc == "bc-d":
                        fallback_offsets.append(p.get("1", {}).get("offset", 0))
        if not fallback and len(completion) == 4:
            break
    with factory() as session:
        signals = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.plan_continue",
            )
        ).all()
        scheduled = {row.payload["continuation_id"] for row in signals}
        assert executed_signals <= scheduled
        assert 0 < len(scheduled) <= turns["regular"]
        assert len(executed_signals) == turns["continuation"]
        assert max(dispatch_costs) >= 1.2
    if fallback:
        # 两个时区账户分别485/484，ceil(/5)之和194；每页5s仅网络下界970s。
        assert reads["fallback"] >= 8
        assert fallback_offsets[-1] > fallback_offsets[3]
        assert "bc-d" not in completion
        assert (97 + 97) * 5 > 15 * 60
        assert all(
            stages[(bc, kind)] > 0 for bc in pools for kind in ("hot", "material")
        )
    else:
        assert len(completion) == 4, (completion, reads, stages)
        assert max(completion.values()) <= 900
        assert background_modes["continuation"] == 0
        assert stages[("bc-c", "history")] > 0
        assert stages[("bc-c", "closeout")] > 0
        assert reads["bc-a"] == 10  # 双日期各5页，不能用一个非法4859行大页替代。
        with factory() as session:
            jobs = session.exec(
                select(ReportSyncJob.kind, ReportSyncJob.advertiser_ids).where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.priority <= 2,
                )
            ).all()
            for kind in ("ad_day", "material_day"):
                assert {a for k, ids in jobs if k == kind for a in ids} == {
                    a for accounts in pools.values() for a in accounts
                }
    print(  # noqa: T201 - 私有日志保留容量模型结果
        {
            "fallback": fallback,
            "entry_counts": dict(turns),
            "scheduled_signals": len(scheduled),
            "dispatch_seconds": sum(dispatch_costs),
            "complete_seconds": completion,
            "pages": dict(reads),
            "fact_batches": {
                str(k): n for k, n in stages.items() if k[1] in {"hot", "material"}
            },
        }
    )  # noqa: T201 - 记录容量模型证据


@pytest.mark.parametrize("operation", ["set", "get", "eval"])
def test_redis_transport_failure_keeps_lease_bounded_and_next_tick_recovers(
    monkeypatch, redis_client, planning_keys, operation
):
    from redis.exceptions import ConnectionError

    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    called = []
    monkeypatch.setattr(
        job_tasks, "_run_planning_entry", lambda **_: called.append(True) or 0
    )
    identity = str(uuid4())
    payload = {
        "continuation_id": identity,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }
    redis_client.set(planning_keys[1], identity, ex=60)
    original = getattr(redis_client, operation)

    def fail(*_a, **_kw):
        raise ConnectionError("test unavailable transport")

    monkeypatch.setattr(redis_client, operation, fail)
    if operation == "eval":
        job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client)
        assert called == [True]
        assert 0 < redis_client.ttl(planning_keys[0]) <= 35
    else:
        with pytest.raises(ConnectionError):
            job_tasks.plan_due_runs(
                database_engine=engine,
                redis_client=redis_client,
                continuation=payload if operation == "get" else None,
            )
        assert not called
    monkeypatch.setattr(redis_client, operation, original)
    # 释放不可用时租约由Redis TTL回收，模拟到期后普通轮可重新取得，不删除业务游标。
    redis_client.pexpire(planning_keys[0], 0)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client)
    assert len(called) == (2 if operation == "eval" else 1)
    assert not redis_client.exists(planning_keys[0])
