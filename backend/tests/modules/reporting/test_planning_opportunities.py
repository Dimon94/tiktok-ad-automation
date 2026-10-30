"""外部筛查得到可用读取片，尾部总预算不足不冒充慢账户惩罚。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.db import engine
from app.modules.reporting import job_tasks, planner
from app.modules.reporting import planning_steps as steps
from app.modules.reporting.query_budget import QueryBudgetExceeded
from app.modules.reporting.store_models import AdSyncBc


def _prepared(session, seed, now, phase):
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    plan.next_directory_at = plan.next_daily_at = now + timedelta(days=1)
    accounts = planner.owned_accounts(session, plan)
    state = steps._prepare(
        plan, accounts, now, planner._timezones(session, plan.tenant_id, accounts)
    )
    state["phase"] = steps.PHASE_ORDER.index(phase)
    plan.screening_progress = {steps.KEY: state}
    return plan


def test_tail_budget_does_not_start_screen_and_prioritizes_its_next_turn(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "screen_hot")
    phases = []
    monkeypatch.setattr(
        steps, "_execute", lambda _s, _p, _v, phase, *_: phases.append(phase)
    )
    steps.plan_step(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now=now,
        allow_backfill=False,
        budget=2.1,
    )
    assert phases == ["hot"]
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert (
            plan.screening_progress[steps.KEY]["screen_waiting_since"]
            == now.isoformat()
        )
        selected = planner.due_plans(session, now=now, limit=1)
        assert selected[0].bc_id == "bc-a"
    steps.plan_step(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now=now,
        allow_backfill=False,
        budget=8,
    )
    assert phases == ["hot", "screen_hot"]
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert "screen_waiting_since" not in plan.screening_progress[steps.KEY]


def test_tail_sql_budget_does_not_shrink_or_delay_batch(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "hot")

    def exhausted(*_args):
        raise QueryBudgetExceeded()

    monkeypatch.setattr(steps, "_execute", exhausted)
    steps.plan_step(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now=now,
        allow_backfill=False,
        budget=2.1,
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        state = plan.screening_progress[steps.KEY]
        assert state["hot"]["size"] == 64
        assert "retry_at" not in state["hot"]
        assert state["phase"] == steps.PHASE_ORDER.index("hot")


def test_fewer_than_two_seconds_ends_entry_without_starting_a_phase(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.modules.reporting import link_work

    seed, _ = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    clock = [0.0]
    monkeypatch.setattr(job_tasks.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        link_work,
        "wake_link_dependencies",
        lambda *_a, **_: clock.__setitem__(0, 8.5),
    )
    calls = []
    monkeypatch.setattr(job_tasks, "_plan_one", lambda *_a, **_kw: calls.append(True))
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    assert not calls


def test_permission_backoff_does_not_keep_screen_priority(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.errors import DomainError

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        plan = _prepared(session, seed, now, "screen_hot")
        state = plan.screening_progress[steps.KEY]
        plan.screening_progress = {
            steps.KEY: {**state, "screen_waiting_since": now.isoformat()}
        }

    def denied(*_args):
        raise DomainError("report_permission_denied", "denied")

    monkeypatch.setattr(steps, "_execute", denied)
    steps.plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=False
    )
    with factory() as session:
        state = session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress[
            steps.KEY
        ]
        assert "screen_waiting_since" not in state
        assert datetime.fromisoformat(state["screen_hot"]["retry_at"]) > now


def test_old_screen_completion_immediately_starts_current_observation(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from tests.modules.reporting.test_job_tasks import _ctx

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    old = now - timedelta(minutes=25)
    with factory() as session, session.begin():
        session.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )
        _prepared(session, seed, old, "screen_hot")

    class Reader:
        def bc_spend(self, **_):
            return RawReportPage((), 1, 1, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))
    steps.plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=False
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.active_screened_at == old
        step = plan.screening_progress[steps.KEY]["screen_hot"]
        assert step["at"] == now.isoformat() and not step["done"]


def test_late_live_completion_immediately_catches_up_without_changing_grid(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    old = now - timedelta(minutes=19)
    with factory() as session, session.begin():
        plan = _prepared(session, seed, old, "material")
        state = plan.screening_progress[steps.KEY]
        state = {**state, "hot": {**state["hot"], "done": True}}
        plan.screening_progress = {steps.KEY: state}
        plan.next_hot_at = plan.next_material_at = now + timedelta(minutes=11)

    def finish(_session, _plan, state, phase, *_args):
        state[phase]["done"] = True

    monkeypatch.setattr(steps, "_execute", finish)
    steps.plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=False
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.next_hot_at == now + timedelta(minutes=11)
        assert plan.screening_progress[steps.KEY].get("force_live") is True
    after = now + timedelta(seconds=30)
    steps.plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=after, allow_backfill=False
    )
    with factory() as session:
        state = session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress[
            steps.KEY
        ]
        assert state["live"]["at"] == after.isoformat()
        assert "force_live" not in state


def test_full_phase_sql_cancel_still_shrinks_batch(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "hot")

    def cancelled(session, *_args):
        session.execute(text("SELECT pg_cancel_backend(pg_backend_pid())"))
        session.execute(text("SELECT 1"))

    monkeypatch.setattr(steps, "_execute", cancelled)
    steps.plan_step(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now=now,
        allow_backfill=False,
        budget=8,
    )
    with factory() as session:
        step = session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress[
            steps.KEY
        ]["hot"]
        assert step["size"] == 32
        assert datetime.fromisoformat(step["retry_at"]) == now + timedelta(seconds=5)


def test_four_bc_screens_rotate_after_maintenance_without_starving_facts(
    monkeypatch, committed_sync_seed, redis_client
):
    from collections import Counter

    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess, TenantBC
    from app.modules.reporting import link_work
    from app.modules.reporting.store_models import AdSyncAccount

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    bcs = ("bc-a", "bc-b", "bc-c", "bc-d")
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        first = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        session.get(AdSyncAccount, (seed.tenant_id, "acc-2")).owner_bc_id = "bc-b"
        for i, bc in enumerate(bcs[2:], start=3):
            session.add(TenantBC(tenant_id=seed.tenant_id, bc_id=bc))
            session.add(
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id=f"acc-{i}",
                    timezone="UTC",
                    currency="USD",
                )
            )
            session.flush()
            # 新规划入口真实核对冻结授权；新增BC必须有绑定及账户授权，不能只造owner。
            session.add(
                BCConnectionBinding(
                    tenant_id=seed.tenant_id,
                    bc_id=bc,
                    connection_id=seed.connection.id,
                    kind="OFFICIAL_API",
                    status="ACTIVE",
                    authorization_revision=0,
                    revision=0,
                )
            )
            session.add(
                BCAccountAccess(
                    tenant_id=seed.tenant_id,
                    bc_id=bc,
                    advertiser_id=f"acc-{i}",
                    connection_id=seed.connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    checked_at=now,
                )
            )
            session.add(
                AdSyncAccount(
                    tenant_id=seed.tenant_id, advertiser_id=f"acc-{i}", owner_bc_id=bc
                )
            )
            session.add(
                AdSyncBc(
                    tenant_id=seed.tenant_id,
                    bc_id=bc,
                    actor_id=seed.actor_id,
                    frozen_route={**first.frozen_route, "bc_id": bc},
                    next_hot_at=now,
                    next_material_at=now,
                    next_daily_at=now + timedelta(days=1),
                    next_directory_at=now + timedelta(days=1),
                )
            )
        session.flush()
        for bc in bcs:
            plan = session.get(AdSyncBc, (seed.tenant_id, bc))
            plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
    clock = [0.0]
    pages = Counter()
    reads = []
    facts = set()
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(steps, "monotonic", lambda: clock[0])
    monkeypatch.setattr(planner, "ensure_bc_plans", lambda *_a, **_kw: None)
    monkeypatch.setattr(job_tasks, "_dispatch_pending", lambda *_a, **_kw: 0)
    monkeypatch.setattr(
        link_work,
        "wake_link_dependencies",
        lambda *_a, **_: clock.__setitem__(0, clock[0] + 2),
    )

    def timed(_session, plan, state, phase, *_args):
        if phase == "screen_hot":
            if _args[-1] < 5:
                from app.core.errors import DomainError

                clock[0] += _args[-1]
                raise DomainError("read_deadline_exceeded", "test SDK deadline")
            pages[plan.bc_id] += 1
            reads.append(plan.bc_id)
            clock[0] += 5
            state[phase]["done"] = pages[plan.bc_id] == 2
        else:
            clock[0] += 0.1
            if phase in {"hot", "material"}:
                facts.add((plan.bc_id, phase))
            else:
                state[phase]["done"] = True

    monkeypatch.setattr(steps, "_execute", timed)
    for tick in range(8):
        started = clock[0]
        job_tasks.plan_due_runs(
            database_engine=engine,
            redis_client=redis_client,
            now=now + timedelta(seconds=30 * tick),
        )
        assert clock[0] - started <= 10.001
        if tick == 3:
            assert set(reads) == set(bcs) and len(reads) == 4
            assert facts == {(bc, kind) for bc in bcs for kind in ("hot", "material")}
    assert pages == dict.fromkeys(bcs, 2)
