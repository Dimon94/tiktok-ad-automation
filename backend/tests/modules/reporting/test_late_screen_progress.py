"""晚完成筛查只补本轮遗漏，不将已提交的事实巡检退回账户池首页。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.db import engine
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import job_tasks, planner
from app.modules.reporting import planning_steps as steps
from app.modules.reporting.store_models import AdReportDay, AdSyncBc, ReportSyncJob
from tests.modules.reporting.test_job_tasks import _ctx


def _seed(factory, seed, accounts, now, *, after, covered, done, advanced):
    with factory() as session, session.begin():
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
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_hot_at = plan.next_material_at = now + timedelta(minutes=15)
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
        state = {
            "live": {
                "at": now.isoformat(),
                "done": done,
                "hot_due": True,
                "clock_advanced": advanced,
            },
            "screen_hot": steps._new(now),
            **{
                p: steps._new(now, after=after, covered=covered, done=done)
                for p in ("hot", "material")
            },
        }
        steps._save(plan, state)


def _run(factory, seed, accounts, now, phase, redis_client):
    from app.modules.sync_control.screening import (
        ScreeningReadRequired,
        screening_responses,
    )

    responses = {}
    for _ in range(2):
        try:
            with screening_responses(responses), factory() as session, session.begin():
                plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
                state = deepcopy(plan.screening_progress[steps.KEY])
                steps._execute(
                    session, plan, state, phase, accounts, now, engine, redis_client, 8
                )
                steps._save(plan, state)
                return state
        except ScreeningReadRequired as request:
            # 和正式plan_step一样，完整退出业务事务后读取，再凭指纹重放。
            with job_tasks._reader_for(
                database_engine=engine,
                redis_client=redis_client,
                plan=request.plan,
                deadline=now + timedelta(seconds=8),
            ) as reader:
                responses[request.fingerprint] = getattr(reader, request.method)(
                    **request.arguments
                )
    raise AssertionError("冻结筛查响应未在第二次事务被消费")


def _reader(monkeypatch, active):
    class Reader:
        def bc_spend(self, **_):
            return RawReportPage(
                tuple(
                    {"dimensions": {"advertiser_id": a}, "metrics": {"spend": "1"}}
                    for a in active
                ),
                1,
                1,
                len(active),
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))


def _jobs(factory, seed):
    with factory() as session:
        return list(
            session.exec(
                select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
            ).all()
        )


def test_4859_finished_accounts_only_need_91_late_additions(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    accounts = [f"late-{i:04d}" for i in range(4859)]
    covered, added = accounts[:2719], accounts[4000:4091]
    _seed(
        factory,
        seed,
        accounts,
        now,
        after=accounts[-1],
        covered=covered,
        done=True,
        advanced=True,
    )
    _reader(monkeypatch, added)
    state = _run(factory, seed, accounts, now, "screen_hot", redis_client)
    assert state["hot"]["after"] == accounts[-1]
    for phase in ("hot", "material"):
        for _ in range(2):
            state = _run(factory, seed, accounts, now, phase, redis_client)
        assert state[phase]["done"]
        assert set(state[phase]["covered"]) == set(covered + added)
    for kind in ("ad_day", "material_day"):
        assert {
            a for j in _jobs(factory, seed) if j.kind == kind for a in j.advertiser_ids
        } == set(added)
    with factory() as session, session.begin():
        for job in session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all():
            job.status = "COMPLETE"
    count = len(_jobs(factory, seed))
    _run(factory, seed, accounts, now, "screen_hot", redis_client)
    for phase in ("hot", "material"):
        state = _run(factory, seed, accounts, now, phase, redis_client)
        assert state[phase]["done"]
    assert len(_jobs(factory, seed)) == count


def test_partial_screen_keeps_tail_known_facts_and_future_single_account_retry(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    accounts = [f"half-{i:02d}" for i in range(8)]
    _seed(
        factory,
        seed,
        accounts,
        now,
        after=accounts[3],
        covered=[accounts[1]],
        done=False,
        advanced=False,
    )
    retry = now + timedelta(minutes=5)
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        state = deepcopy(plan.screening_progress[steps.KEY])
        for phase in ("hot", "material"):
            state[phase]["deferred"] = {accounts[2]: retry.isoformat()}
        steps._save(plan, state)
        session.add(
            AdReportDay(
                tenant_id=seed.tenant_id,
                advertiser_id=accounts[7],
                stat_date=now.date(),
                ad_id="known-zero",
                campaign_id="c",
                adgroup_id="g",
                currency="USD",
                timezone="UTC",
                fetched_at=now,
                sync_job_id=uuid4(),
                spend=0,
            )
        )
    _reader(monkeypatch, [accounts[0], accounts[2], accounts[6]])
    state = _run(factory, seed, accounts, now, "screen_hot", redis_client)
    assert state["hot"]["after"] == accounts[3]
    assert state["hot"]["deferred"][accounts[2]] == retry.isoformat()
    for phase in ("hot", "material"):
        state = _run(factory, seed, accounts, now, phase, redis_client)
        assert not state[phase]["done"]
        assert set(state[phase]["covered"]) == {accounts[i] for i in (0, 1, 6, 7)}
        state = _run(factory, seed, accounts, retry, phase, redis_client)
        assert state[phase]["done"]
        assert accounts[2] in state[phase]["covered"]


@pytest.mark.parametrize("after", ["", "recover-0040"])
def test_persisted_advanced_live_recovers_without_rescanning_inactive_pool(
    committed_sync_seed, redis_client, after
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    accounts = [f"recover-{i:04d}" for i in range(200)]
    _seed(
        factory,
        seed,
        accounts,
        now,
        after=after,
        covered=accounts[:100],
        done=False,
        advanced=True,
    )
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = accounts[:100] + [accounts[180]]
    for phase in ("hot", "material"):
        state = _run(factory, seed, accounts, now, phase, redis_client)
        assert state[phase]["done"]
        assert set(state[phase]["covered"]) == set(accounts[:100] + [accounts[180]])
    assert {a for j in _jobs(factory, seed) for a in j.advertiser_ids} == {
        accounts[180]
    }


def test_late_screen_does_not_open_hot_when_only_material_was_due(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    accounts = ["material-only-0", "material-only-1"]
    _seed(
        factory,
        seed,
        accounts,
        now,
        after=accounts[-1],
        covered=[accounts[0]],
        done=True,
        advanced=True,
    )
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        state = deepcopy(plan.screening_progress[steps.KEY])
        state["live"]["hot_due"] = False
        state["hot"] = steps._new(now, done=True)
        steps._save(plan, state)
    _reader(monkeypatch, [accounts[1]])
    state = _run(factory, seed, accounts, now, "screen_hot", redis_client)
    assert state["hot"]["done"]
    assert not state["hot"]["deferred"]
    state = _run(factory, seed, accounts, now, "material", redis_client)
    assert state["material"]["done"]
    assert {j.kind for j in _jobs(factory, seed)} == {"material_day"}


def test_old_screen_after_new_live_keeps_current_day_and_tail_known_facts(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC).replace(hour=0, minute=1)
    old = now - timedelta(minutes=2)
    accounts = ["midnight-0", "midnight-1", "midnight-2"]
    _seed(
        factory,
        seed,
        accounts,
        now,
        after=accounts[0],
        covered=[],
        done=False,
        advanced=False,
    )
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        state = deepcopy(plan.screening_progress[steps.KEY])
        state["screen_hot"] = steps._new(old)
        # 新live不继承上一live的完成证明，未扫尾部必须继续发现零消耗事实。
        state["live"].pop("clock_advanced")
        steps._save(plan, state)
        session.add(
            AdReportDay(
                tenant_id=seed.tenant_id,
                advertiser_id=accounts[2],
                stat_date=now.date(),
                ad_id="midnight-known",
                campaign_id="c",
                adgroup_id="g",
                currency="USD",
                timezone="UTC",
                fetched_at=now,
                sync_job_id=uuid4(),
                spend=0,
            )
        )
    _reader(monkeypatch, [accounts[0]])
    state = _run(factory, seed, accounts, now, "screen_hot", redis_client)
    assert state["hot"]["at"] == now.isoformat()
    for phase in ("hot", "material"):
        state = _run(factory, seed, accounts, now, phase, redis_client)
        assert state[phase]["done"]
        assert set(state[phase]["covered"]) == {accounts[0], accounts[2]}
    assert all(
        job.start_date == job.end_date == now.date() for job in _jobs(factory, seed)
    )
