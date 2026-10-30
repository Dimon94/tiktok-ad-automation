"""规划保留独立提交，同时避免重复扫描账户元数据和重复写整份进度。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.core.db import engine
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import job_tasks, planner
from app.modules.reporting import planning_steps as steps
from app.modules.reporting.store_models import AdSyncAccount, AdSyncBc
from tests.modules.reporting.test_job_tasks import _ctx
from tests.modules.reporting.test_planning_opportunities import _prepared


def test_stable_owner_refresh_does_not_create_new_row_versions(committed_sync_seed):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
    sql = text(
        "SELECT advertiser_id,xmin::text,xmax::text,ctid::text FROM ad_sync_account WHERE tenant_id=:t ORDER BY advertiser_id"
    )
    with factory() as session:
        before = session.execute(sql, {"t": seed.tenant_id}).all()
    with factory() as session, session.begin():
        planner._refresh_owners(session, tenant_id=seed.tenant_id)
    with factory() as session:
        assert session.execute(sql, {"t": seed.tenant_id}).all() == before
    # 归属真正改变仍须修正，不能把去无用写变成只插入不更新。
    with factory() as session, session.begin():
        session.get(AdSyncAccount, (seed.tenant_id, "acc-1")).owner_bc_id = "bc-b"
    with factory() as session, session.begin():
        planner._refresh_owners(session, tenant_id=seed.tenant_id)
        assert (
            session.get(AdSyncAccount, (seed.tenant_id, "acc-1")).owner_bc_id == "bc-a"
        )


@pytest.mark.parametrize("phase", ["hot", "screen_hot"])
def test_successful_phase_writes_progress_once(
    monkeypatch, committed_sync_seed, redis_client, caplog, phase
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, phase)

    class Reader:
        def bc_spend(self, **_):
            return RawReportPage((), 1, 1, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))
    monkeypatch.setattr(steps.logger, "disabled", False)
    updates = []

    def count(_conn, _cursor, statement, *_):
        if statement.startswith("UPDATE ad_sync_bc SET"):
            updates.append(statement)

    event.listen(engine, "after_cursor_execute", count)
    try:
        with caplog.at_level("INFO", logger=steps.logger.name):
            assert steps.plan_step(
                engine,
                redis_client,
                (seed.tenant_id, "bc-a"),
                now=now,
                allow_backfill=False,
            )
    finally:
        event.remove(engine, "after_cursor_execute", count)
    assert len(updates) == 1
    assert "metadata_ms=" in caplog.text
    assert "work_ms=" in caplog.text and "total_ms=" in caplog.text
    assert "acc-1" not in caplog.text


def test_4859_account_phase_reads_metadata_once(committed_sync_seed, redis_client):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    bulk = [f"cost-{i:04d}" for i in range(4857)]
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
                for a in bulk
            ]
        )
        session.flush()
        session.add_all(
            [
                AdSyncAccount(
                    tenant_id=seed.tenant_id, advertiser_id=a, owner_bc_id="bc-a"
                )
                for a in bulk
            ]
        )
        session.flush()
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.next_daily_at = plan.next_directory_at = now + timedelta(days=1)
        accounts = planner.owned_accounts(session, plan)
        assert len(accounts) == 4859
        plan.last_active_advertiser_ids = accounts
        state = steps._prepare(
            plan, accounts, now, planner._timezones(session, seed.tenant_id, accounts)
        )
        state["phase"] = steps.PHASE_ORDER.index("hot")
        steps._save(plan, state)
    rows = []
    visited = []
    inspected = []

    def explain(conn, _cursor, statement, parameters, *_):
        if "/* planning_owned_metadata */" in statement and not inspected:
            inspected.append(True)
            tree = conn.exec_driver_sql(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, parameters
            ).scalar_one()[0]["Plan"]

            def visit(node):
                if node.get("Relation Name") == "advertiser_account":
                    visited.append(
                        (
                            node.get("Actual Rows", 0)
                            + node.get("Rows Removed by Filter", 0)
                        )
                        * node.get("Actual Loops", 1)
                    )
                for child in node.get("Plans", []):
                    visit(child)

            visit(tree)

    def count(_conn, cursor, statement, *_):
        # 归属集合和元数据已分成两条窄查询；本断言追踪实际元数据读取，
        # 不能继续匹配旧CTE而把归属行数冒充时区查询成本。
        if "/* planning_owned_metadata */" in statement and not statement.startswith(
            "EXPLAIN"
        ):
            rows.append(cursor.rowcount)

    event.listen(engine, "before_cursor_execute", explain)
    event.listen(engine, "after_cursor_execute", count)
    try:
        assert steps.plan_step(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now=now,
            allow_backfill=False,
        )
    finally:
        event.remove(engine, "after_cursor_execute", count)
        event.remove(engine, "before_cursor_execute", explain)
    assert rows == [4859]
    assert visited and sum(visited) <= 4859, visited


def test_timezone_reuse_is_transaction_and_tenant_scoped(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=datetime.now(UTC))
    with factory() as session:
        with session.begin():
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            values = planner.owned_account_timezones(session, plan)
            assert values["acc-1"] == "Asia/Hong_Kong"
            assert planner._timezones(session, uuid4(), ["acc-1"]) == {}
            session.add(
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id="new-meta",
                    timezone="Asia/Tokyo",
                    currency="USD",
                )
            )
            session.flush()
            assert planner._timezones(session, seed.tenant_id, ["new-meta"]) == {
                "new-meta": "Asia/Tokyo"
            }
        with factory() as writer, writer.begin():
            writer.get(AdvertiserAccount, (seed.tenant_id, "acc-1")).timezone = "UTC"
        with session.begin():
            assert planner._timezones(session, seed.tenant_id, ["acc-1"]) == {
                "acc-1": "UTC"
            }


def test_timezone_reuse_does_not_survive_rollback(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=datetime.now(UTC))
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        planner.owned_account_timezones(session, plan)
        session.rollback()
        with factory() as writer, writer.begin():
            writer.get(AdvertiserAccount, (seed.tenant_id, "acc-1")).timezone = "UTC"
        assert planner._timezones(session, seed.tenant_id, ["acc-1"]) == {
            "acc-1": "UTC"
        }


def _cancel_progress_write_once():
    fired = []

    def cancel(_conn, _cursor, statement, *_):
        if statement.startswith("UPDATE ad_sync_bc SET") and not fired:
            fired.append(True)
            _conn.exec_driver_sql("SELECT pg_cancel_backend(pg_backend_pid())")
            _conn.exec_driver_sql("SELECT 1")

    return cancel, fired


def test_final_progress_cancel_rolls_back_clock_and_current_jobs_only(
    committed_sync_seed, redis_client
):
    from copy import deepcopy

    from sqlmodel import select

    from app.modules.reporting.store_models import ReportSyncJob

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        plan = _prepared(session, seed, now, "material")
        plan.last_active_advertiser_ids = ["acc-1"]
        state = deepcopy(plan.screening_progress[steps.KEY])
        state["hot"].update(done=True, after="acc-2", covered=["acc-1"])
        steps._save(plan, state)
        # 模拟上一阶段已经独立提交，最后一次进度写取消不能撤销它。
        planner.plan_hot(session, plan, active=["acc-1"], now=now)
        original_clocks = (plan.next_hot_at, plan.next_material_at, plan.next_daily_at)
    cancel, fired = _cancel_progress_write_once()
    metrics = {}
    event.listen(engine, "before_cursor_execute", cancel)
    try:
        assert steps.plan_step(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now=now,
            allow_backfill=False,
            metrics=metrics,
        )
    finally:
        event.remove(engine, "before_cursor_execute", cancel)
    assert fired and metrics.get("completed", 0) == 0 and metrics["deferred"] == 1
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert (
            plan.next_hot_at,
            plan.next_material_at,
            plan.next_daily_at,
        ) == original_clocks
        state = plan.screening_progress[steps.KEY]
        assert not state["material"]["done"]
        assert not state["live"].get("clock_advanced")
        assert {
            j.kind
            for j in session.exec(
                select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
            ).all()
        } == {"ad_day"}


def test_screen_page_is_not_committed_when_final_progress_write_is_cancelled(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "screen_hot")
    pages = []

    class Reader:
        def bc_spend(self, *, page, **_):
            pages.append(page)
            return RawReportPage(
                (
                    {
                        "dimensions": {"advertiser_id": "acc-1"},
                        "metrics": {"spend": "1"},
                    },
                ),
                1,
                2,
                2,
            )

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))
    cancel, fired = _cancel_progress_write_once()
    event.listen(engine, "before_cursor_execute", cancel)
    try:
        steps.plan_step(
            engine,
            redis_client,
            (seed.tenant_id, "bc-a"),
            now=now,
            allow_backfill=False,
        )
    finally:
        event.remove(engine, "before_cursor_execute", cancel)
    assert fired and pages == [1]
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert "bc:1" not in plan.screening_progress
        assert plan.active_screened_at is None
        assert not plan.screening_progress[steps.KEY]["screen_hot"]["done"]


def test_entry_logs_successful_stage_count_and_budget_components(
    monkeypatch, committed_sync_seed, redis_client, caplog
):
    import re

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        _prepared(session, seed, now, "screen_hot")

    class Reader:
        def bc_spend(self, **_):
            return RawReportPage((), 1, 1, 0)

    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: _ctx(Reader()))
    monkeypatch.setattr(steps.logger, "disabled", False)
    monkeypatch.setattr(job_tasks.logger, "disabled", False)
    with (
        caplog.at_level("INFO", logger=steps.logger.name),
        caplog.at_level("INFO", logger=job_tasks.logger.name),
    ):
        job_tasks.plan_due_runs(
            database_engine=engine, redis_client=redis_client, now=now
        )
    entries = [
        r.getMessage()
        for r in caplog.records
        if "report planning entry finished:" in r.getMessage()
    ]
    assert len(entries) == 1
    entry = entries[0]
    completed = sum(
        "report planning phase finished:" in r.getMessage()
        and "completed=True" in r.getMessage()
        for r in caplog.records
    )
    assert int(re.search(r"completed=(\d+)", entry)[1]) == completed > 0
    for name in (
        "prepare_ms",
        "maintenance_ms",
        "dispatch_ms",
        "steps_ms",
        "metadata_ms",
        "work_ms",
        "total_ms",
        "step_budget",
    ):
        assert name + "=" in entry
    assert "acc-1" not in entry and "frozen_route" not in entry


def test_owner_refresh_locks_changed_accounts_only(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=datetime.now(UTC))
    available = text(
        "SELECT advertiser_id FROM ad_sync_account WHERE tenant_id=:t FOR UPDATE SKIP LOCKED"
    )
    with factory() as writer, writer.begin():
        planner._refresh_owners(writer, tenant_id=seed.tenant_id)
        with factory() as reader, reader.begin():
            assert set(reader.execute(available, {"t": seed.tenant_id}).scalars()) == {
                "acc-1",
                "acc-2",
            }
    with factory() as session, session.begin():
        session.get(AdSyncAccount, (seed.tenant_id, "acc-1")).owner_bc_id = "bc-b"
    with factory() as writer, writer.begin():
        planner._refresh_owners(writer, tenant_id=seed.tenant_id)
        with factory() as reader, reader.begin():
            assert set(reader.execute(available, {"t": seed.tenant_id}).scalars()) == {
                "acc-2"
            }
    with factory() as session:
        assert (
            session.get(AdSyncAccount, (seed.tenant_id, "acc-1")).owner_bc_id == "bc-a"
        )
