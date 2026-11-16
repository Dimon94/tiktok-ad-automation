from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.operation_logs.models import (
    OperationCoverage,
    OperationExport,
    OperationSyncCursor,
)
from app.modules.operation_logs.planning import plan_logs
from app.modules.reporting.store_models import AdSyncAccount, AdSyncBc, ReportSyncJob
from app.modules.sync_control.models import SyncRequirement
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed
from tests.modules.reporting.test_jobs import _route


def test_recent_and_history_have_separate_cursors_and_shared_account_owner(
    session, sync_seed
):
    seed = sync_seed
    session.add(
        AdSyncAccount(
            tenant_id=seed.tenant_id, advertiser_id="acc-1", owner_bc_id="bc-a"
        )
    )
    session.add(
        AdSyncAccount(
            tenant_id=seed.tenant_id, advertiser_id="acc-2", owner_bc_id="bc-a"
        )
    )
    route = _route(seed)
    plan = AdSyncBc(
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=route,
    )
    other = AdSyncBc(
        tenant_id=seed.tenant_id,
        bc_id="bc-b",
        actor_id=seed.actor_id,
        frozen_route={**route, "bc_id": "bc-b"},
    )
    session.flush()
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    assert plan_logs(session, plan, now=now, history_start=date(2026, 8, 1)) == 4
    assert plan_logs(session, other, now=now, history_start=date(2026, 8, 1)) == 0
    rows = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
    ).all()
    assert all(r.kind == "operation_log" and len(r.advertiser_ids) == 1 for r in rows)
    assert all((r.end_date - r.start_date).days < 29 for r in rows)
    assert sorted({r.priority for r in rows}) == [3, 4]
    assert plan_logs(session, plan, now=now, history_start=date(2026, 8, 1)) == 0
    assert session.get(
        OperationSyncCursor, (seed.tenant_id, "acc-1")
    ).recent_day == date(2026, 11, 10)

    assert plan_logs(session, plan, now=now, history_start=date(2026, 7, 1)) == 0
    rows = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
    ).all()
    assert all(
        r.start_date == r.end_date == date(2026, 11, 9) for r in rows if r.priority == 3
    )
    history = sorted((r.start_date, r.end_date) for r in rows if r.priority == 4)
    assert history[-1] == (date(2026, 10, 11), date(2026, 11, 8))
    assert len(history) == 2  # 每账户只留一个历史窗口


@pytest.fixture
def log_plan(session, sync_seed):
    seed = sync_seed
    session.add(
        AdSyncAccount(
            tenant_id=seed.tenant_id, advertiser_id="acc-1", owner_bc_id="bc-a"
        )
    )
    session.flush()
    return AdSyncBc(
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
    )


def test_completed_dates_split_gaps_and_pending_do_not_inflate_revisions(
    session, log_plan
):
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    exported = OperationExport(
        tenant_id=log_plan.tenant_id,
        advertiser_id="acc-1",
        bc_id="bc-a",
        frozen_route=log_plan.frozen_route,
        task_id="done",
        start_at=now - timedelta(days=10),
        end_at=now,
        timezone="Asia/Hong_Kong",
        observed_at=now,
        digest="a" * 64,
        archive_key="private/test",
        byte_length=1,
        parser_version=1,
        event_count=0,
    )
    session.add(exported)
    session.flush()
    for day in [6, 9]:
        session.add(
            OperationCoverage(
                tenant_id=log_plan.tenant_id,
                advertiser_id="acc-1",
                stat_date=date(2026, 11, day),
                export_id=exported.id,
                digest="a" * 64,
                checked_at=now,
            )
        )
    session.flush()
    assert plan_logs(session, log_plan, now=now, history_start=date(2026, 11, 1)) == 1
    row = session.exec(select(ReportSyncJob)).one()
    assert (row.start_date, row.end_date) == (date(2026, 11, 7), date(2026, 11, 8))
    assert (
        plan_logs(
            session,
            log_plan,
            now=now + timedelta(minutes=1),
            history_start=date(2026, 11, 1),
        )
        == 0
    )
    assert (
        plan_logs(
            session,
            log_plan,
            now=now + timedelta(minutes=2),
            history_start=date(2026, 11, 1),
        )
        == 0
    )
    requirements = session.exec(select(SyncRequirement)).all()
    assert all(r.desired_revision == 1 for r in requirements)
    assert all(r.bucket_key not in {"2026-11-06", "2026-11-09"} for r in requirements)


def test_yesterday_uses_account_timezone_and_failed_retry_waits_until_next_day(
    session, log_plan
):
    now = datetime(2026, 11, 10, 17, tzinfo=UTC)  # 香港已11日
    assert plan_logs(session, log_plan, now=now) == 1
    row = session.exec(select(ReportSyncJob)).one()
    assert row.start_date == row.end_date == date(2026, 11, 10)
    row.status = "FAILED"
    session.flush()
    assert (
        plan_logs(
            session,
            log_plan,
            now=now + timedelta(minutes=1),
            history_start=date(2026, 11, 10),
        )
        == 0
    )
    assert (
        plan_logs(
            session,
            log_plan,
            now=now + timedelta(days=1),
            history_start=date(2026, 11, 10),
        )
        == 2
    )
    assert (
        max(r.desired_revision for r in session.exec(select(SyncRequirement)).all())
        == 2
    )


def test_standalone_planner_ignores_blocked_report_screening_and_rotates(
    session, sync_seed
):
    from app.jobs.celery_app import celery_app
    from app.modules.operation_logs.tasks import plan_next_batch

    seed = sync_seed
    now = datetime(2026, 11, 16, 1, tzinfo=UTC)
    plan = AdSyncBc(
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
        next_daily_at=now - timedelta(days=2),
        next_hot_at=now,
        next_material_at=now,
        next_directory_at=now,
        screening_progress={
            "planning_steps_v1": {"daily": {"done": False, "at": now.isoformat()}}
        },
    )
    session.add(plan)
    for advertiser in ["acc-1", "acc-2"]:
        session.add(
            AdSyncAccount(
                tenant_id=seed.tenant_id, advertiser_id=advertiser, owner_bc_id="bc-a"
            )
        )
    session.flush()
    assert plan_next_batch(session, now=now, batch_size=1) == 1
    assert plan_next_batch(session, now=now + timedelta(seconds=1), batch_size=1) == 1
    assert plan_next_batch(session, now=now + timedelta(seconds=2), batch_size=1) == 0
    rows = session.exec(select(ReportSyncJob)).all()
    assert {tuple(r.advertiser_ids) for r in rows} == {("acc-1",), ("acc-2",)}
    assert all(r.start_date == r.end_date == date(2026, 11, 15) for r in rows)
    assert not plan.screening_progress["planning_steps_v1"]["daily"]["done"]
    assert plan.next_daily_at == now - timedelta(days=2)
    entry = celery_app.conf.beat_schedule["plan-operation-logs"]
    assert (
        entry["task"] == "operation_logs.plan"
        and entry["options"]["queue"] == "control"
    )
    assert entry["options"]["expires"] <= entry["schedule"]
    plan.enabled = False
    session.flush()
    assert plan_next_batch(session, now=now + timedelta(days=1)) == 0


def test_history_resumes_after_inflight_window_finishes(session, log_plan):
    now = datetime(2026, 11, 16, 1, tzinfo=UTC)
    assert plan_logs(session, log_plan, now=now, history_start=date(2026, 7, 1)) == 2
    history = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.priority == 4)
    ).one()
    assert (
        plan_logs(
            session,
            log_plan,
            now=now + timedelta(seconds=1),
            history_start=date(2026, 7, 1),
        )
        == 0
    )
    history.status = "FAILED"
    session.flush()
    assert (
        plan_logs(
            session,
            log_plan,
            now=now + timedelta(seconds=2),
            history_start=date(2026, 7, 1),
        )
        == 1
    )
    histories = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.priority == 4)
    ).all()
    assert histories[1].end_date < history.start_date


def test_standalone_task_respects_sync_switch(monkeypatch):
    from app.modules.operation_logs import tasks

    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", False)
    monkeypatch.setattr(
        tasks, "Session", lambda _: pytest.fail("disabled must not open database")
    )
    assert tasks.plan_operation_logs() == 0
