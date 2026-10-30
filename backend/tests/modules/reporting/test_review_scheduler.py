from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.core.db import engine
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting import job_tasks, planner
from app.modules.reporting.store_models import AdSyncAccount, AdSyncBc

NOW = datetime(2026, 10, 12, 13, tzinfo=UTC)


def test_empty_plan_does_not_consume_scan_limit(session, sync_seed):
    planner.ensure_bc_plans(session, now=NOW)
    session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b")).next_hot_at = NOW - timedelta(
        days=1
    )
    session.flush()
    assert [p.bc_id for p in planner.due_plans(session, now=NOW, limit=1)] == ["bc-a"]


@pytest.mark.parametrize("retry_ms", [300000, 86400000])
def test_daily_admission_waits_until_retry_deadline(
    monkeypatch, committed_sync_seed, redis_client, retry_ms
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as s:
        planner.ensure_bc_plans(s, now=NOW)
        plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_hot_at = NOW + timedelta(days=2)
        plan.next_daily_at = NOW
        s.commit()
    calls = []

    class Reader:
        def bc_spend(self, **kwargs):
            calls.append(kwargs)
            raise AccountAdmissionDeferred(retry_ms)

    @contextmanager
    def reader_for(**_kwargs):
        yield Reader()

    monkeypatch.setattr(job_tasks, "_reader_for", reader_for)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=NOW)
    job_tasks.plan_due_runs(
        database_engine=engine,
        redis_client=redis_client,
        now=NOW + timedelta(seconds=30),
    )
    assert len(calls) == 1


def test_scan_commits_completed_plan_before_later_timeout(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    original = planner.ensure_bc_plans

    def ensure(s, **kwargs):
        result = original(s, **kwargs)
        s.get(AdSyncAccount, (seed.tenant_id, "acc-2")).owner_bc_id = "bc-b"
        s.get(AdSyncBc, (seed.tenant_id, "bc-a")).next_hot_at = NOW - timedelta(
            seconds=1
        )
        s.flush()
        return result

    monkeypatch.setattr(planner, "ensure_bc_plans", ensure)
    deadlines = []
    opened_at = []

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

    @contextmanager
    def reader_for(*, plan, deadline, **_kwargs):
        opened_at.append(datetime.now(UTC))
        deadlines.append(deadline)
        if plan.bc_id == "bc-b":
            raise SoftTimeLimitExceeded()
        yield Reader()

    monkeypatch.setattr(job_tasks, "_reader_for", reader_for)
    entry_started = datetime.now(UTC)
    try:
        job_tasks.plan_due_runs(
            database_engine=engine, redis_client=redis_client, now=NOW
        )
    except SoftTimeLimitExceeded:
        pass
    with factory() as s:
        plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        # 跨BC按阶段轮转，第二BC超时前第一BC的筛查已独立提交，事实尚待其下一轮。
        assert plan is not None and plan.active_screened_at == NOW
        assert plan.last_active_advertiser_ids == ["acc-1"]
    # 每个BC读取预算受剩余总预算约束，前阶段提交不能被另一BC超时撤销。
    assert len(deadlines) == 2
    # 统一入口共享10秒剩余预算，不能为后一个BC重新赠送固定8秒。
    assert all(
        deadline <= entry_started + timedelta(seconds=10) for deadline in deadlines
    )
    assert all(
        timedelta(seconds=2) <= deadline - started <= timedelta(seconds=10)
        for deadline, started in zip(deadlines, opened_at, strict=True)
    )


def test_account_fallback_resumes_completed_requests_after_deadline(session, sync_seed):
    from app.core.errors import DomainError

    planner.ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.bc_report_supported = False
    calls = []

    class Reader:
        fail = True

        def account_day(self, **kwargs):
            calls.append((kwargs["advertiser_ids"], kwargs["start"], kwargs["end"]))
            if self.fail and len(calls) == 2:
                raise DomainError("read_deadline_exceeded", "合成读取期限耗尽")
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": kwargs["advertiser_ids"][0]},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                1,
                1,
            )

    reader = Reader()
    with pytest.raises(DomainError):
        job_tasks._active(session, reader, plan, ["acc-1", "acc-2"], NOW, days=32)
    session.flush()
    session.expire(plan)
    reader.fail = False
    assert job_tasks._active(session, reader, plan, ["acc-1", "acc-2"], NOW, days=32)
    assert calls.count(calls[0]) == 1


def test_two_thousand_account_fallback_eventually_finishes_without_repeating_pages(
    session, sync_seed
):
    from app.core.errors import DomainError
    from app.modules.accounts.models import AdvertiserAccount

    planner.ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.bc_report_supported = False
    accounts = [f"large-{i:04}" for i in range(2000)]
    session.add_all(
        [
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=a,
                currency="USD",
                timezone="UTC",
            )
            for a in accounts
        ]
    )
    session.flush()
    successful = []

    class Reader:
        remaining = 0

        def account_day(self, **kw):
            if not self.remaining:
                raise DomainError("read_deadline_exceeded", "本轮合成时间预算耗尽")
            self.remaining -= 1
            successful.append((kw["advertiser_ids"], kw["start"], kw["end"]))
            return RawReportPage(
                tuple(
                    {"dimensions": {"advertiser_id": a}, "metrics": {"spend": "1"}}
                    for a in kw["advertiser_ids"]
                ),
                1,
                1,
                len(kw["advertiser_ids"]),
            )

    reader = Reader()
    result = None
    for _ in range(8):
        reader.remaining = 100
        try:
            result = job_tasks._active(session, reader, plan, accounts, NOW, days=32)
        except DomainError:
            session.flush()
            session.expire(plan)
        else:
            break
    assert result == accounts
    assert len(successful) == len(set(successful)) == 800
