"""健康救援名额只分配给未覆盖缺口，按当前当地日最老水位优先。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting import health, planner
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob, ReportSyncState

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def _case(session, seed, count):
    planner.ensure_bc_plans(session, now=NOW, tenant_id=seed.tenant_id)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    session.execute(
        text("""
        INSERT INTO advertiser_account
          (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
        SELECT :tenant,'repair-'||lpad(i::text,3,'0'),'','USD',
          CASE WHEN i>=50 AND i%2=1 THEN 'Pacific/Kiritimati' ELSE 'UTC' END,
          'ENABLE',false FROM generate_series(0,:last) i
        """),
        {"tenant": seed.tenant_id, "last": count - 1},
    )
    from tests.modules.reporting.sync_conftest import grant_accounts

    accounts = [f"repair-{i:03d}" for i in range(count)]
    grant_accounts(session, seed, accounts)
    return plan, accounts


def _pending(session, plan, report_kind, accounts, **changes):
    values = {
        "tenant_id": plan.tenant_id,
        "bc_id": plan.bc_id,
        "actor_id": plan.actor_id,
        "frozen_route": plan.frozen_route,
        "advertiser_ids": accounts,
        "kind": report_kind,
        "priority": 1 if report_kind == "ad_day" else 2,
        "status": "QUEUED",
        "start_date": NOW.date(),
        "end_date": NOW.date(),
        "job_key": str(uuid4()),
        "due_at": NOW + timedelta(hours=1),
    }
    job = ReportSyncJob(**(values | changes))
    session.add(job)
    session.flush()
    return job


def _repair(session, plan, kind, accounts):
    repairs = {
        name: set()
        for name in ("ad", "material", "directory", "campaign_day", "adgroup_day")
    }
    repairs["ad" if kind == "ad_day" else "material"] = set(accounts)
    health._repair_health(
        session, plan, SimpleNamespace(last_repair_at=None), repairs, NOW
    )
    session.flush()


@pytest.mark.parametrize("kind", ["ad_day", "creative_day"])
def test_inflight_first_fifty_do_not_consume_repair_slots(session, sync_seed, kind):
    plan, accounts = _case(session, sync_seed, 110)
    pending = [
        _pending(session, plan, kind, accounts[part:50:3], status=status)
        for part, status in enumerate(("QUEUED", "RUNNING", "WAITING"))
    ]
    original_ids = {job.id for job in pending}
    for i, account in enumerate(accounts):
        day = NOW.date() + timedelta(days=int(i >= 50 and i % 2 == 1))
        session.add(
            ReportSyncState(
                tenant_id=plan.tenant_id,
                advertiser_id=account,
                report_kind=kind,
                stat_date=day,
                status="COMPLETE",
                last_success_at=None if i == 109 else NOW - timedelta(minutes=120 + i),
            )
        )
    session.flush()
    _repair(session, plan, kind, accounts)
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == plan.tenant_id)
    ).all()
    fresh = [job for job in jobs if job.id not in original_ids and job.kind == kind]
    # 两个当地日合计最多50，缺水位在先，其余按年龄；不能按ID取前50。
    assert {account for job in fresh for account in job.advertiser_ids} == set(
        accounts[60:]
    )
    assert sum(len(job.advertiser_ids) for job in fresh) == 50
    assert all(
        job.start_date
        == job.end_date
        == NOW.date() + timedelta(days=int(int(job.advertiser_ids[0][-3:]) % 2 == 1))
        for job in fresh
    )
    assert all(job.due_at == NOW + timedelta(hours=1) for job in pending)
    # 后续救援只处理剩余10个；再重复不会复制已在途工作，也不会刷新成功水位。
    _repair(session, plan, kind, accounts)
    after = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == plan.tenant_id)
    ).all()
    _repair(session, plan, kind, accounts)
    assert len(
        session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == plan.tenant_id)
        ).all()
    ) == len(after)
    assert sum(len(job.advertiser_ids) for job in after if job.kind == kind) == 110
    assert (
        session.get(
            ReportSyncState,
            (plan.tenant_id, accounts[109], kind, NOW.date() + timedelta(days=1)),
        ).last_success_at
        is None
    )


@pytest.mark.parametrize(
    "difference",
    [
        "tenant",
        "route",
        "actor",
        "bc",
        "kind",
        "window",
        "campaign",
        "priority",
        "terminal",
    ],
)
def test_other_scope_or_terminal_work_cannot_hide_missing_fact(
    session, sync_seed, monkeypatch, difference
):
    plan, accounts = _case(session, sync_seed, 2)
    changes = {
        "tenant": {"tenant_id": uuid4()},
        "route": {"frozen_route": plan.frozen_route | {"binding_revision": 999}},
        "actor": {"actor_id": uuid4()},
        "bc": {"bc_id": "other-bc"},
        "kind": {"kind": "creative_day"},
        "window": {"start_date": NOW.date() - timedelta(days=1)},
        "campaign": {"campaign_ids": ["campaign"]},
        "priority": {"priority": 3},
        "terminal": {"status": "COMPLETE"},
    }[difference]
    previous = _pending(session, plan, "ad_day", [accounts[0]], **changes)
    session.add(
        ReportSyncState(
            tenant_id=plan.tenant_id,
            advertiser_id=accounts[1],
            report_kind="ad_day",
            stat_date=NOW.date(),
            status="COMPLETE",
            last_success_at=NOW - timedelta(hours=1),
        )
    )
    session.flush()
    monkeypatch.setattr(health, "MAX_REPAIR_ACCOUNTS", 1)
    _repair(session, plan, "ad_day", accounts)
    new = [
        job
        for job in session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == plan.tenant_id)
        ).all()
        if job.id != previous.id and job.kind == "ad_day" and job.kind == "ad_day"
    ]
    assert len(new) == 1 and new[0].advertiser_ids == [accounts[0]]


def test_missing_watermark_precedes_oldest_success_and_ignores_other_dates(
    session, sync_seed, monkeypatch
):
    plan, accounts = _case(session, sync_seed, 3)
    for account, day, age in [
        (accounts[0], NOW.date(), 30),
        (accounts[1], NOW.date(), 60),
        (accounts[2], NOW.date() - timedelta(days=1), 0),
    ]:
        session.add(
            ReportSyncState(
                tenant_id=plan.tenant_id,
                advertiser_id=account,
                report_kind="ad_day",
                stat_date=day,
                status="COMPLETE",
                last_success_at=NOW - timedelta(minutes=age),
            )
        )
    session.flush()
    monkeypatch.setattr(health, "MAX_REPAIR_ACCOUNTS", 1)
    _repair(session, plan, "ad_day", accounts)
    jobs = session.exec(
        select(ReportSyncJob)
        .where(ReportSyncJob.tenant_id == plan.tenant_id)
        .where(ReportSyncJob.kind == "ad_day")
    ).all()
    assert len(jobs) == 1 and jobs[0].advertiser_ids == [accounts[2]]
    _repair(session, plan, "ad_day", accounts)
    jobs = session.exec(
        select(ReportSyncJob)
        .where(ReportSyncJob.tenant_id == plan.tenant_id)
        .where(ReportSyncJob.kind == "ad_day")
    ).all()
    assert len(jobs) == 2
    assert {account for job in jobs for account in job.advertiser_ids} == set(
        accounts[1:]
    )
