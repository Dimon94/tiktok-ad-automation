"""健康测量与修复分开预算，已有待领关联由专用证据唤醒流程复核。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.modules.reporting import health, planner
from app.modules.reporting.store_models import (
    AdSyncBc,
    CreativeReportDay,
    ReportSyncHealth,
    ReportSyncState,
)


def _case(session, seed):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    plan.active_screened_at = now
    plan.last_active_advertiser_ids = ["acc-2"]
    session.add(
        CreativeReportDay(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-2",
            stat_date=now.date(),
            creative_id="post",
            campaign_id="c1",
            adgroup_id="g1",
            currency="USD",
            timezone="UTC",
            fetched_at=now,
            sync_job_id=uuid4(),
            spend=1,
        )
    )
    for kind in ("ad_day", "creative_day"):
        session.add(
            ReportSyncState(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-2",
                report_kind=kind,
                stat_date=now.date(),
                status="COMPLETE",
                row_count=1,
                last_success_at=now,
            )
        )
    session.flush()
    return now, plan


def test_repair_budget_preserves_measurement_and_rolls_back_slow_repair(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed)
    monkeypatch.setattr(health, "REPAIR_BUDGET_SECONDS", 0.05, raising=False)
    completed = []

    def slow(*args, **_kwargs):
        args[0].execute(text("SELECT pg_sleep(0.2)"))
        completed.append(True)
        return 1

    monkeypatch.setattr(health, "plan_hot", slow)
    row = health.observe_health(session, plan, now=now)
    assert not completed
    assert row.metrics["unmapped_creatives"] == 1 and row.observed_at == now
    assert row.last_repair_at is None
    assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_expired_total_budget_restores_savepoint_and_statement_timeout(
    session, monkeypatch
):
    from importlib import import_module

    from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget

    module = import_module("app.modules.reporting.query_budget")
    session.execute(text("SET LOCAL statement_timeout='750ms'"))
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    with pytest.raises(QueryBudgetExceeded), session.begin_nested():
        with query_budget(session, seconds=0.2):
            assert session.execute(text("SELECT 1")).scalar_one() == 1
            clock[0] = 0.21
            session.execute(text("SELECT 2"))
    assert session.execute(text("SHOW statement_timeout")).scalar_one() == "750ms"
    assert session.execute(text("SELECT 3")).scalar_one() == 3


def test_unknown_candidate_timeout_preserves_freshness_and_previous_coverage(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed)
    plan.active_screened_at = None
    coverage_at = (now - timedelta(minutes=1)).isoformat()
    session.add(
        ReportSyncHealth(
            tenant_id=plan.tenant_id,
            bc_id=plan.bc_id,
            observed_at=now - timedelta(minutes=1),
            metrics={
                "coverage_status": "COMPLETE",
                "coverage_observed_at": coverage_at,
                "coverage_completed_at": coverage_at,
                "coverage_scope": "scope",
                "missing_directory_ads": 9,
                "unmapped_creatives": 8,
            },
        )
    )
    session.flush()
    original = session.execute
    nested = []

    def slow_candidates(statement, *args, **kwargs):
        if "ORDER BY s.last_success_at" in str(statement):
            nested.append(session.in_nested_transaction())
            with health.query_budget(session, seconds=0.05):
                original(text("SELECT pg_sleep(1)"))
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", slow_candidates)
    measurement = health.measure_freshness(session, plan, now=now)
    row = health.observe_health(
        session,
        plan,
        now=now,
        measurement=measurement,
        phase="freshness",
        scope="scope",
    )
    assert nested == [True]
    assert row.observed_at == now
    assert (
        row.metrics["stale_ad_accounts"] == row.metrics["stale_material_accounts"] == 0
    )
    assert row.metrics["missing_directory_ads"] == 9
    assert row.metrics["unmapped_creatives"] == 8
    assert row.metrics["coverage_status"] == "COMPLETE"
    assert row.metrics["coverage_observed_at"] == coverage_at
    assert row.status == "DEGRADED" and "ACCOUNT_SCREEN_STALE" in row.issues
    assert row.last_repair_at is None
    assert original(text("SELECT 1")).scalar_one() == 1


def test_coverage_repair_does_not_repeat_unknown_account_selection(
    session, sync_seed, monkeypatch
):
    now, plan = _case(session, sync_seed)
    plan.active_screened_at = None
    session.add(
        ReportSyncHealth(
            tenant_id=plan.tenant_id,
            bc_id=plan.bc_id,
            observed_at=now,
            issues=["ACCOUNT_SCREEN_STALE"],
            metrics={"coverage_status": "PENDING", "coverage_scope": "scope"},
        )
    )
    session.flush()
    original = session.execute

    def reject_candidates(statement, *args, **kwargs):
        assert "ORDER BY s.last_success_at" not in str(statement)
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", reject_candidates)
    row = health.observe_health(
        session,
        plan,
        now=now,
        phase="coverage",
        scope="scope",
        measurement=(
            {"missing_directory_ads": 0, "unmapped_creatives": 0},
            [],
            {
                name: set()
                for name in ("ad", "material", "directory", *health.PARENT_KINDS)
            },
        ),
    )
    assert "ACCOUNT_SCREEN_STALE" in row.issues
    assert row.metrics["coverage_status"] == "COMPLETE"
