"""后阶段失败不能撤销已提交的筛查和实时规划。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.core.db import engine
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting import job_tasks, planner
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob, ReportSyncState
from tests.modules.reporting.test_job_tasks import _ctx


def test_published_state_sizes_batches_without_counting_fact_rows(session, sync_seed):
    now = datetime.now(UTC)
    for account in ("acc-1", "acc-2"):
        session.add(
            ReportSyncState(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=account,
                report_kind="ad_day",
                stat_date=now.date(),
                status="COMPLETE",
                row_count=8000,
                last_success_at=now,
            )
        )
    session.flush()
    assert planner.batch_accounts(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1", "acc-2"],
        days=1,
    ) == [["acc-1"], ["acc-2"]]


def test_late_sql_cancel_keeps_screen_and_both_live_facts(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_directory_at = plan.next_daily_at = now + timedelta(days=1)

    class Reader:
        def bc_spend(self, **kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))

    def cancelled(session, *_args, **_kwargs):
        session.execute(text("SELECT pg_cancel_backend(pg_backend_pid())"))
        session.execute(text("SELECT 1"))

    monkeypatch.setattr(planner, "plan_day_closeout", cancelled)
    try:
        job_tasks._plan_one(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now,
            datetime.now(UTC) + timedelta(seconds=8),
            planner.HOT_INTERVAL,
            planner.MATERIAL_INTERVAL,
            allow_backfill=False,
        )
    except DBAPIError:
        pass
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.active_screened_at == now
        kinds = set(
            session.exec(
                select(ReportSyncJob.kind).where(
                    ReportSyncJob.tenant_id == seed.tenant_id
                )
            ).all()
        )
        assert {"ad_day", "material_day"} <= kinds


@pytest.mark.parametrize("page_seconds", [0, 5])
def test_thousands_of_accounts_progress_many_batches_and_cover_both_facts(
    monkeypatch, committed_sync_seed, redis_client, page_seconds
):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.reporting.store_models import AdSyncAccount

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    accounts = [f"bulk-{i:04d}" for i in range(4859)]
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        session.add_all(
            [
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id=a,
                    timezone="UTC",
                    currency="USD",
                )
                for a in accounts
            ]
        )
        session.flush()
        session.add_all(
            [
                BCAccountAccess(
                    tenant_id=seed.tenant_id,
                    bc_id="bc-a",
                    advertiser_id=a,
                    connection_id=seed.connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    checked_at=now,
                )
                for a in accounts
            ]
        )
        session.add_all(
            [
                AdSyncAccount(
                    tenant_id=seed.tenant_id, advertiser_id=a, owner_bc_id="bc-a"
                )
                for a in accounts
            ]
        )
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
        plan.last_active_advertiser_ids = accounts
        small = session.get(AdSyncBc, (seed.tenant_id, "bc-b"))
        small.next_daily_at = small.next_directory_at = now + timedelta(days=1)
        small.last_active_advertiser_ids = ["acc-2"]
        session.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )
        access = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == seed.tenant_id,
                BCAccountAccess.bc_id == "bc-a",
                BCAccountAccess.advertiser_id == "acc-2",
            )
        ).one()
        access.active = False
    pages = []
    from app.modules.reporting import planning_steps as steps

    clock = [0.0]
    if page_seconds:
        monkeypatch.setattr(job_tasks.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(steps, "monotonic", lambda: clock[0])
        execute = steps._execute

        def timed(*args):
            result = execute(*args)
            if not args[3].startswith("screen_"):
                clock[0] += 0.5
            return result

        monkeypatch.setattr(steps, "_execute", timed)

    read_budget = [8.0]

    class Reader:
        def bc_spend(self, *, bc_id, page, **kwargs):
            if page_seconds:
                if read_budget[0] < page_seconds:
                    from app.core.errors import DomainError

                    clock[0] += read_budget[0]
                    raise DomainError("read_deadline_exceeded", "test SDK deadline")
                clock[0] += page_seconds
            pages.append((bc_id, page))
            values = accounts if bc_id == "bc-a" else ["acc-2"]
            return RawReportPage(
                tuple(
                    {"dimensions": {"advertiser_id": a}, "metrics": {"spend": "1"}}
                    for a in values[(page - 1) * 1000 : page * 1000]
                ),
                page,
                (len(values) + 999) // 1000,
                len(values),
            )

    def reader_for(**kwargs):
        read_budget[0] = (kwargs["deadline"] - datetime.now(UTC)).total_seconds()
        return _ctx(Reader())

    monkeypatch.setattr(job_tasks, "_reader_for", reader_for)
    from time import monotonic

    samples = []
    screened_at_attempt = None
    for attempt in range(30 if page_seconds else 4):
        started = monotonic()
        job_tasks.plan_due_runs(
            database_engine=engine,
            redis_client=redis_client,
            now=now + timedelta(seconds=30 * attempt),
        )
        with factory() as session:
            rows = session.exec(
                select(ReportSyncJob.kind, ReportSyncJob.advertiser_ids).where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.priority <= 2,
                )
            ).all()
            covered = {
                kind: {a for k, ids in rows if k == kind for a in ids}
                for kind in ("ad_day", "material_day")
            }
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            samples.append(
                {
                    "attempt": attempt + 1,
                    "elapsed": round(monotonic() - started, 2),
                    "covered": {k: len(v) for k, v in covered.items()},
                    "phases": {
                        k: {f: v.get(f) for f in ("after", "done", "retry_at")}
                        for k, v in plan.screening_progress["planning_steps_v1"].items()
                        if isinstance(v, dict)
                    },
                }
            )
            if attempt == 0:
                assert len(covered["ad_day"]) > (0 if page_seconds else 64)
                assert len(covered["material_day"]) > (0 if page_seconds else 64)
                assert (
                    "acc-2" in covered["ad_day"] and "acc-2" in covered["material_day"]
                )
            screened = plan.screening_progress["planning_steps_v1"]["screen_hot"][
                "done"
            ]
            if screened and screened_at_attempt is None:
                screened_at_attempt = attempt + 1
            if all(set(accounts) <= values for values in covered.values()) and screened:
                break
    # 独立记录规划覆盖/逻辑轮次；pytest总耗时包含造数与清理，不能当平台吞吐。
    import json

    print(  # noqa: T201
        "SYNC_PLANNING_TRACE "
        + json.dumps(
            {
                "fixture_accounts": 4859,
                "simulated_transport_seconds_per_page": page_seconds,
                "screened_at_attempt": screened_at_attempt,
                "logical_round_interval_seconds": 30,
                "bc_a_pages": [page for bc, page in pages if bc == "bc-a"],
                "samples": samples,
            },
            default=str,
        )
    )

    assert all(set(accounts) <= values for values in covered.values()), samples
    assert screened, samples
    if page_seconds:
        assert screened_at_attempt <= 6, samples
        assert attempt * 30 < 15 * 60, samples
    assert [page for bc, page in pages if bc == "bc-a"] == [1, 2, 3, 4, 5]


def test_soft_timeout_does_not_replay_completed_screen_or_hot_batch(
    monkeypatch, committed_sync_seed, redis_client
):
    import pytest
    from billiard.exceptions import SoftTimeLimitExceeded

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    calls = []
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
        session.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )

    class Reader:
        def bc_spend(self, **kwargs):
            calls.append(kwargs["page"])
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    original = planner.plan_material

    def timeout(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(planner, "plan_material", timeout)
    with pytest.raises(SoftTimeLimitExceeded):
        job_tasks._plan_one(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now,
            now + timedelta(seconds=3),
            planner.HOT_INTERVAL,
            planner.MATERIAL_INTERVAL,
            allow_backfill=False,
        )
    with factory() as session:
        before = set(
            session.exec(
                select(ReportSyncJob.id).where(
                    ReportSyncJob.tenant_id == seed.tenant_id
                )
            ).all()
        )
        assert before
        assert session.get(AdSyncBc, (seed.tenant_id, "bc-a")).active_screened_at == now
    monkeypatch.setattr(planner, "plan_material", original)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now,
        now + timedelta(seconds=3),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    assert calls == [1]
    with factory() as session:
        rows = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
        assert {r.id for r in rows if r.kind == "ad_day"} == before
        assert any(r.kind == "material_day" for r in rows)


def test_history_shards_resume_before_marking_account_complete(session, sync_seed):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.store_models import AdSyncAccount

    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    account = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-1"))
    account.remote_created_at = now - timedelta(days=100)
    progress = {}
    planner.plan_history(session, plan, now=now, accounts=["acc-1"], progress=progress)
    assert "acc-1" in progress
    assert (
        session.get(AdSyncAccount, (sync_seed.tenant_id, "acc-1")).history_planned_at
        is None
    )
    for _ in range(4):
        planner.plan_history(
            session, plan, now=now, accounts=["acc-1"], progress=progress
        )
    assert not progress
    assert (
        session.get(AdSyncAccount, (sync_seed.tenant_id, "acc-1")).history_planned_at
        == now
    )


def test_late_screen_page_only_adds_new_accounts_after_prior_jobs_complete(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.modules.reporting.planning_steps import KEY, plan_step

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
        plan.last_active_advertiser_ids = ["acc-1"]
        session.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )

    class Reader:
        def bc_spend(self, *, page, **kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": f"acc-{page}"},
                        "metrics": {"spend": "1"},
                    },
                ),
                page,
                2,
                2,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    for _ in range(30):
        if not plan_step(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now=now,
            allow_backfill=False,
        ):
            break
        with factory() as session, session.begin():
            for row in session.exec(
                select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
            ).all():
                row.status = "COMPLETE"
    with factory() as session:
        for kind in ("ad_day", "material_day"):
            rows = session.exec(
                select(ReportSyncJob).where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.kind == kind,
                )
            ).all()
            assert sorted(a for row in rows for a in row.advertiser_ids) == [
                "acc-1",
                "acc-2",
            ]
        assert session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress[KEY][
            "live"
        ]["done"]


def test_delayed_empty_proof_keeps_its_observation_time(session, sync_seed):
    now = datetime.now(UTC)
    proof = now - timedelta(hours=2)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    planner.mark_confirmed_empty(
        session,
        plan,
        inactive=["acc-2"],
        window_fn=lambda tz: planner._hot_window(tz, now),
        now=now,
        observed_at=proof,
    )
    state = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-2", "ad_day", now.date())
    )
    assert state.last_success_at == proof
    state.last_success_at = now
    session.flush()
    planner.mark_confirmed_empty(
        session,
        plan,
        inactive=["acc-2"],
        window_fn=lambda tz: planner._hot_window(tz, now),
        now=now,
        observed_at=proof,
    )
    assert state.last_success_at == now


def test_idle_plan_does_not_hide_due_bc_at_selection_limit(session, sync_seed):
    from app.modules.reporting.planning_steps import KEY
    from app.modules.reporting.store_models import AdSyncAccount

    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    session.get(AdSyncAccount, (sync_seed.tenant_id, "acc-2")).owner_bc_id = "bc-b"
    idle = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    idle.next_hot_at = idle.next_material_at = idle.next_daily_at = (
        idle.next_directory_at
    ) = now + timedelta(days=1)
    idle.screening_progress = {
        KEY: {"pending": False, "last_turn": (now - timedelta(days=1)).isoformat()}
    }
    due = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    due.screening_progress = {KEY: {"pending": True, "last_turn": now.isoformat()}}
    session.flush()
    assert [p.bc_id for p in planner.due_plans(session, now=now, limit=1)] == ["bc-b"]


def test_rule_phase_changed_between_commits_is_not_overwritten(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.reporting.planning_steps import plan_step

    seed, factory = committed_sync_seed
    now = datetime(2026, 10, 30, 0, tzinfo=UTC)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)

    class Reader:
        def bc_spend(self, **kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    assert plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=False
    )
    with factory() as session, session.begin():
        session.add(
            AutomationRule(
                tenant_id=seed.tenant_id,
                name="phase change",
                created_by=seed.actor_id,
                updated_by=seed.actor_id,
                enabled=True,
                definition={"accounts": {"mode": "BC", "bc_ids": ["bc-a"]}},
            )
        )
        session.flush()
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        planner.align_rule_sync_phase(session, plan, now=now + timedelta(minutes=5))
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now + timedelta(minutes=5),
        now + timedelta(minutes=5, seconds=3),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.next_hot_at == plan.next_material_at == now + timedelta(minutes=12)


def test_old_screen_finishes_after_new_live_day_without_reverting_fact_window(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    seed, factory = committed_sync_seed
    before = datetime(2026, 10, 29, 23, 50, tzinfo=UTC)
    allow_second = False
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=before)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = before + timedelta(days=2)
        plan.last_active_advertiser_ids = ["acc-1"]
        session.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )

    class Reader:
        def bc_spend(self, *, page, **kwargs):
            if page == 2 and not allow_second:
                raise AccountAdmissionDeferred(60000)
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": f"acc-{page}"},
                        "metrics": {"spend": "1"},
                    },
                ),
                page,
                2,
                2,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    for minutes in (0, 20):
        at = before + timedelta(minutes=minutes)
        job_tasks._plan_one(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            at,
            at + timedelta(seconds=3),
            planner.HOT_INTERVAL,
            planner.MATERIAL_INTERVAL,
            allow_backfill=False,
        )
    allow_second = True
    at = before + timedelta(minutes=22)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        at,
        at + timedelta(seconds=3),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    with factory() as session:
        rows = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.priority.in_([1, 2]),
            )
        ).all()
        added = [r for r in rows if "acc-2" in r.advertiser_ids]
        assert {r.kind for r in added} == {"ad_day", "material_day"}
        assert all(r.start_date == r.end_date == at.date() for r in added)


def test_unknown_timezone_waits_for_metadata_without_blocking_valid_accounts(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.planning_steps import KEY

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
        session.get(AdvertiserAccount, (seed.tenant_id, "acc-2")).timezone = ""

    class Reader:
        def bc_spend(self, **kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now,
        now + timedelta(seconds=3),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    with factory() as session:
        rows = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
        assert {r.kind for r in rows} == {"ad_day", "material_day"}
        assert all(r.advertiser_ids == ["acc-1"] for r in rows)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.active_screened_at is None
        assert plan.screening_progress[KEY]["metadata_missing"] == ["acc-2"]


def test_scope_expansion_schedules_new_account_history_and_preserves_rule_grid(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.reporting.planning_steps import KEY

    seed, factory = committed_sync_seed
    now = datetime(2026, 10, 30, 0, tzinfo=UTC)

    class Reader:
        def bc_spend(self, **kwargs):
            return RawReportPage(
                tuple(
                    {"dimensions": {"advertiser_id": a}, "metrics": {"spend": "1"}}
                    for a in ("acc-1", "acc-2", "acc-new")
                ),
                1,
                1,
                3,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now,
        now + timedelta(seconds=3),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    with factory() as session, session.begin():
        session.add(
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-new",
                timezone="UTC",
                currency="USD",
                remote_created_at=now - timedelta(days=100),
            )
        )
        session.flush()
        session.add(
            BCAccountAccess(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                advertiser_id="acc-new",
                connection_id=seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=now,
            )
        )
        session.add(
            AutomationRule(
                tenant_id=seed.tenant_id,
                name="new account phase",
                created_by=seed.actor_id,
                updated_by=seed.actor_id,
                enabled=True,
                definition={"accounts": {"mode": "BC", "bc_ids": ["bc-a"]}},
            )
        )
        session.flush()
        planner.ensure_bc_plans(session, now=now + timedelta(minutes=5))
        assert session.get(AdSyncBc, (seed.tenant_id, "bc-a")).screening_progress[KEY][
            "pending"
        ]
    at = now + timedelta(minutes=5)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        at,
        at + timedelta(seconds=5),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.next_hot_at == plan.next_material_at == now + timedelta(minutes=12)
        rows = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
        required = {now.date() - timedelta(days=i) for i in range(101)}
        for kind in ("ad_day", "material_day"):
            covered = {
                r.start_date + timedelta(days=i)
                for r in rows
                if r.kind == kind and "acc-new" in r.advertiser_ids
                for i in range((r.end_date - r.start_date).days + 1)
            }
            assert required <= covered


def test_actual_entry_passes_only_remaining_budget_after_prior_phase(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, _ = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    clock = [0.0]
    monkeypatch.setattr(job_tasks.time, "monotonic", lambda: clock[0])

    class Reader:
        def bc_spend(self, **kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    original = job_tasks._plan_one
    budgets = []

    def timed(*args, **kwargs):
        budgets.append(kwargs["time_budget"])
        result = original(*args, **kwargs)
        clock[0] += 7.5 if len(budgets) == 1 else 1
        return result

    monkeypatch.setattr(job_tasks, "_plan_one", timed)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    assert len(budgets) == 2
    assert budgets[0] == 10 and budgets[1] <= 2.5


def test_daily_completion_crossing_midnight_immediately_catches_up(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.modules.reporting.planning_steps import KEY, _new, plan_step, scope_digest

    seed, factory = committed_sync_seed
    before = datetime(2026, 10, 29, 23, 59, tzinfo=UTC)
    after = before + timedelta(minutes=2)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=before)
        session.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_hot_at = plan.next_material_at = plan.next_directory_at = (
            after + timedelta(days=1)
        )
        accounts = planner.owned_accounts(session, plan)
        plan.screening_progress = {
            KEY: {
                "scope": scope_digest(
                    plan,
                    accounts,
                    planner._timezones(session, plan.tenant_id, accounts),
                ),
                "phase": 6,
                "screen_daily": _new(before, done=True),
                "daily": _new(before, done=True, active=["acc-1"]),
                "empty_daily": _new(before, active=["acc-1"]),
            }
        }
    plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=after, allow_backfill=True
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.next_daily_at == after

    class Reader:
        def bc_spend(self, **kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        after,
        after + timedelta(seconds=3),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=True,
    )
    with factory() as session:
        rows = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
        assert {
            row.kind
            for row in rows
            if row.end_date == before.date()
            and "acc-1" in row.advertiser_ids
            and row.kind in {"ad_day", "material_day"}
        } == {"ad_day", "material_day"}


def test_live_batches_get_a_turn_between_each_background_phase(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.modules.reporting import planning_steps as steps

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        accounts = planner.owned_accounts(session, plan)
        plan.screening_progress = {
            steps.KEY: {
                "scope": steps.scope_digest(
                    plan,
                    accounts,
                    planner._timezones(session, plan.tenant_id, accounts),
                ),
                "phase": 0,
                "live": {"at": now.isoformat(), "hot_due": True},
                **{phase: steps._new(now) for phase in steps.PHASES},
            }
        }
    phases = []
    monkeypatch.setattr(
        steps,
        "_execute",
        lambda _session, _plan, _state, phase, *_args: phases.append(phase),
    )
    for _ in range(12):
        steps.plan_step(
            engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=True
        )
    assert phases.count("hot") == phases.count("material") == 4
    assert phases.count("screen_hot") == 2
    for _ in range(30):
        steps.plan_step(
            engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=True
        )
    assert set(phases) == set(steps.PHASES)

    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        state = plan.screening_progress[steps.KEY]
        state = {
            **state,
            "screen_hot": {**state["screen_hot"], "done": True},
            "screen_daily": {**state["screen_daily"], "done": True},
        }
        plan.screening_progress = {steps.KEY: state}
    clock = [0.0]
    phases.clear()
    monkeypatch.setattr(steps, "monotonic", lambda: clock[0])

    def slow_background(_session, _plan, _state, phase, *_args):
        phases.append(phase)
        budget = _args[-1]
        clock[0] += min(budget, 0.05 if phase in {"hot", "material"} else 3)

    monkeypatch.setattr(steps, "_execute", slow_background)
    # 入口10秒中维护已花3秒；每个后台批用满3秒，实时规划0.05秒/64户。
    steps.run_plan(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now=now,
        deadline=now + timedelta(seconds=10),
        allow_backfill=True,
        time_budget=7,
    )
    assert phases.count("hot") >= 4 and phases.count("material") >= 4
    assert clock[0] <= 7.001
