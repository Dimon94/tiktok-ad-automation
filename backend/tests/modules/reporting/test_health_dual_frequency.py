"""分钟新鲜度与完整覆盖分别发布，重检查不能阻塞或伪造轻量水位。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text

from app.modules.reporting import health
from app.modules.reporting.store_models import AdSyncBc, ReportSyncHealth
from tests.modules.reporting.test_health_continuation import _case


def test_minute_only_measures_freshness_and_enqueues_coverage(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    sql = []

    def capture(_conn, _cursor, statement, *_args):
        sql.append(" ".join(statement.lower().split()))

    event.listen(args["database_engine"], "before_cursor_execute", capture)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert progress["offset"] == 0
        assert not any("with posts as materialized" in query for query in sql)
        assert not any("r.ad_id,r.adgroup_id,r.campaign_id" in query for query in sql)
        assert (
            sum("select count(*) from management_task" in query for query in sql) == 1
        )
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["stale_ad_accounts"] == 1
            assert row.metrics["coverage_status"] == "PENDING"
            assert "COVERAGE_PENDING" in row.issues
    finally:
        event.remove(args["database_engine"], "before_cursor_execute", capture)
        redis_client.delete(key)


def test_coverage_completion_does_not_move_freshness_time_and_is_not_repeated(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    try:
        health.run_bc_health(**args)
        with factory() as session:
            fresh_at = session.get(
                ReportSyncHealth, (seed.tenant_id, "bc-a")
            ).observed_at
        cycle = json.loads(redis_client.get(key))["cycle"]
        assert health.run_bc_health(**args, cycle=cycle) == "CONTINUE"
        assert health.run_bc_health(**args, cycle=cycle) == "COMPLETE"
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.observed_at == fresh_at
            assert row.metrics["coverage_status"] == "COMPLETE"
            coverage_at = row.metrics["coverage_observed_at"]
        assert health.run_bc_health(**args) == "COMPLETE"
        assert redis_client.get(key) is None
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["coverage_observed_at"] == coverage_at
            assert row.observed_at >= fresh_at
    finally:
        redis_client.delete(key)


@pytest.mark.parametrize(
    "age,due,stale", [(179, False, False), (181, True, False), (300, True, True)]
)
def test_coverage_starts_after_three_minutes_and_expires_at_five(
    committed_sync_seed, redis_client, monkeypatch, age, due, stale
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        scope = health._health_scope(
            session, plan, health.owned_accounts(session, plan)
        )
        session.add(
            ReportSyncHealth(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                observed_at=now,
                metrics={
                    "coverage_status": "COMPLETE",
                    "coverage_scope": scope,
                    "coverage_observed_at": (now - timedelta(seconds=age)).isoformat(),
                    "coverage_completed_at": (
                        now - timedelta(seconds=age - 1)
                    ).isoformat(),
                    "missing_directory_ads": 0,
                    "unlinked_material_posts": 0,
                },
            )
        )
    try:
        assert health.run_bc_health(**args) == ("CONTINUE" if due else "COMPLETE")
        assert bool(redis_client.get(key)) == due
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert ("COVERAGE_STALE" in row.issues) == stale
            assert (
                row.metrics["coverage_observed_at"]
                == (now - timedelta(seconds=age)).isoformat()
            )
    finally:
        redis_client.delete(key)


def test_scope_tracks_timezone_day_rules_and_ignores_screen_timestamp(
    committed_sync_seed, monkeypatch
):
    from app.modules.automation_rules.models import AutomationRule

    seed, factory = committed_sync_seed
    _case(seed, factory, monkeypatch)
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        accounts = health.owned_accounts(session, plan)
        initial = health._health_scope(session, plan, accounts, now=now)
        plan.active_screened_at = now
        assert health._health_scope(session, plan, accounts, now=now) == initial
        assert (
            health._health_scope(session, plan, accounts, now=now + timedelta(days=1))
            != initial
        )
        session.execute(
            text(
                "UPDATE advertiser_account SET timezone='Pacific/Honolulu' WHERE tenant_id=:t AND advertiser_id='acc-2'"
            ),
            {"t": seed.tenant_id},
        )
        changed_zone = health._health_scope(session, plan, accounts, now=now)
        assert changed_zone != initial
        session.add(
            AutomationRule(
                tenant_id=seed.tenant_id,
                name="health scope",
                enabled=True,
                created_by=seed.actor_id,
                updated_by=seed.actor_id,
                definition={"accounts": {"mode": "ALL"}},
            )
        )
        session.flush()
        assert health._health_scope(session, plan, accounts, now=now) != changed_zone


def test_failed_database_commit_keeps_private_complete_progress_until_retry(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_MAX_CHUNKS", 16)
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    assert health.run_bc_health(**args) == "CONTINUE"
    cycle = json.loads(redis_client.get(key))["cycle"]
    fail_once = [True]

    def fail_commit(_conn):
        if fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("simulated commit failure")

    event.listen(args["database_engine"], "commit", fail_commit)
    try:
        assert health.run_bc_health(**args, cycle=cycle) == "FAILED"
        assert json.loads(redis_client.get(key))["offset"] == 2
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["coverage_status"] == "FAILED"
            assert row.metrics["coverage_observed_at"] is None
            assert "missing_directory_ads" not in row.metrics
        assert health.run_bc_health(**args, cycle=cycle) == "COMPLETE"
        assert redis_client.get(key) is None
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["coverage_status"] == "COMPLETE"
            assert row.metrics["missing_directory_ads"] == 0
    finally:
        event.remove(args["database_engine"], "commit", fail_commit)
        redis_client.delete(key)


def test_redis_loss_restarts_full_coverage_and_cannot_publish_partial_counts(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    try:
        health.run_bc_health(**args)
        old = json.loads(redis_client.get(key))["cycle"]
        assert health.run_bc_health(**args, cycle=old) == "CONTINUE"
        redis_client.delete(key)
        assert health.run_bc_health(**args, cycle=old) == "IGNORED"
        assert health.run_bc_health(**args) == "CONTINUE"
        current = json.loads(redis_client.get(key))
        assert current["cycle"] != old and current["offset"] == 0
        assert current["metrics"] == {}
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["coverage_status"] == "PENDING"
            assert "missing_directory_ads" not in row.metrics
    finally:
        redis_client.delete(key)


def test_interleaved_stage_merges_keep_newer_freshness_and_coverage(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    _case(seed, factory, monkeypatch)
    now = datetime.now(UTC)
    repairs = {"ad": set(), "material": set(), "links": set()}
    fresh = {
        "stale_ad_accounts": 0,
        "stale_material_accounts": 0,
        "pending_actions": 0,
        "oldest_data_at": None,
    }
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        health.observe_health(
            session,
            plan,
            now=now,
            phase="freshness",
            scope="scope",
            measurement=(fresh, [], repairs),
        )
    with factory() as reader:
        plan = reader.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        old = reader.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
        assert old.observed_at == now
        with factory() as writer, writer.begin():
            current = writer.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            health.observe_health(
                writer,
                current,
                now=now + timedelta(seconds=10),
                phase="freshness",
                scope="scope",
                measurement=(
                    fresh | {"stale_ad_accounts": 7},
                    ["stale_ad_accounts"],
                    repairs,
                ),
            )
        row = health.observe_health(
            reader,
            plan,
            now=now,
            phase="coverage",
            scope="scope",
            measurement=(
                {"missing_directory_ads": 2, "unlinked_material_posts": 3},
                [],
                repairs,
            ),
        )
        assert row.metrics["stale_ad_accounts"] == 7
        assert row.observed_at == now + timedelta(seconds=10)
        assert row.metrics["missing_directory_ads"] == 2
        assert "stale_ad_accounts" in row.issues
        reader.commit()
        health.observe_health(
            reader,
            plan,
            now=now + timedelta(seconds=5),
            phase="freshness",
            scope="scope",
            measurement=(fresh, [], repairs),
        )
        health.observe_health(
            reader,
            plan,
            now=now - timedelta(seconds=1),
            phase="coverage",
            scope="scope",
            measurement=(
                {"missing_directory_ads": 99, "unlinked_material_posts": 99},
                [],
                repairs,
            ),
        )
        reader.commit()
        reader.refresh(row)
        assert row.metrics["stale_ad_accounts"] == 7
        assert row.metrics["missing_directory_ads"] == 2
        assert row.observed_at == now + timedelta(seconds=10)
        assert row.metrics["coverage_observed_at"] == now.isoformat()


def test_scope_changed_during_freshness_cannot_publish_or_enqueue_old_scope(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    original = health.measure_freshness

    def change_scope(*args, **kwargs):
        result = original(*args, **kwargs)
        with factory() as writer, writer.begin():
            plan = writer.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            plan.frozen_route = plan.frozen_route | {"binding_revision": 99}
        return result

    monkeypatch.setattr(health, "measure_freshness", change_scope)
    assert health.run_bc_health(**args) == "IGNORED"
    assert redis_client.get(key) is None
    with factory() as session:
        assert session.get(ReportSyncHealth, (seed.tenant_id, "bc-a")) is None


def test_failed_minute_still_schedules_complete_coverage_without_claiming_freshness(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_MAX_CHUNKS", 16)
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    before = datetime.now(UTC) - timedelta(minutes=1)
    with factory() as session, session.begin():
        session.add(
            ReportSyncHealth(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                observed_at=before,
                metrics={"stale_material_accounts": 7},
            )
        )

    def failed(db, *_args, **_kwargs):
        with health.query_budget(db, seconds=0.05):
            db.execute(text("SELECT pg_sleep(1)"))

    monkeypatch.setattr(health, "measure_freshness", failed)
    try:
        assert health.run_bc_health(**args) == "FAILED"
        progress = json.loads(redis_client.get(key))
        assert progress["offset"] == 0
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "COMPLETE"
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.observed_at == before
            assert row.metrics["stale_material_accounts"] == 7
            assert row.metrics["coverage_status"] == "COMPLETE"
            assert row.status == "DEGRADED" and "MONITOR_QUERY_FAILED" in row.issues
    finally:
        redis_client.delete(key)


def test_failed_coverage_scheduling_does_not_roll_back_minute_failure_record(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")

    def failed(db, *_args, **_kwargs):
        with health.query_budget(db, seconds=0.05):
            db.execute(text("SELECT pg_sleep(1)"))

    called = []

    def failed_schedule(db, *_args, **_kwargs):
        called.append(db.in_nested_transaction())
        db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(health, "measure_freshness", failed)
    monkeypatch.setattr(health, "_schedule_coverage", failed_schedule)
    try:
        assert health.run_bc_health(**args) == "FAILED"
        assert called == [True]
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.status == "DEGRADED" and "MONITOR_QUERY_FAILED" in row.issues
            assert not row.metrics.get("coverage_observed_at")
            assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        redis_client.delete(key)


def test_later_freshness_batch_failure_preserves_previous_complete_metrics(
    committed_sync_seed, redis_client, monkeypatch
):
    from sqlalchemy.orm import Session

    from tests.modules.reporting.test_health_adaptive_batches import _pool

    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    with factory() as session, session.begin():
        session.execute(
            text("""
            INSERT INTO advertiser_account(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
            SELECT :tenant,'fresh-batch-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,513) i
        """),
            {"tenant": seed.tenant_id},
        )
        session.execute(
            text("""
            INSERT INTO ad_sync_account(tenant_id,advertiser_id,owner_bc_id)
            SELECT :tenant,'fresh-batch-'||i,'bc-a' FROM generate_series(1,513) i
        """),
            {"tenant": seed.tenant_id},
        )
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = health.owned_accounts(session, plan)
        old = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).observed_at
    original = Session.execute
    batches = []

    def fail_second(db, statement, *args, **kwargs):
        if "WITH active AS" in str(statement):
            parameters = kwargs.get("params", args[0] if args else {})
            batches.append(len(parameters["ids"]))
            if len(batches) == 2:
                with health.query_budget(db, seconds=0.05):
                    original(db, text("SELECT pg_sleep(1)"))
        return original(db, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", fail_second)
    try:
        assert health.run_bc_health(**args) == "FAILED"
        assert batches == [512, 129]
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.observed_at == old
            assert row.metrics == {"prior_complete": 1}
            assert "MONITOR_QUERY_FAILED" in row.issues
        # 覆盖可独立开始，但没有把首批512个缺水位的部分计数写入正式指标。
        assert json.loads(redis_client.get(key))["offset"] == 0
    finally:
        redis_client.delete(key)


def test_failed_coverage_is_not_cleared_by_successful_freshness(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    try:
        health.run_bc_health(**args)
        progress = json.loads(redis_client.get(key))
        progress["at"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
        redis_client.set(key, json.dumps(progress), ex=180)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "FAILED"
        assert health.run_bc_health(**args) == "CONTINUE"
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.status == "DEGRADED"
            assert row.metrics["coverage_status"] == "FAILED"
            assert "COVERAGE_QUERY_FAILED" in row.issues
    finally:
        redis_client.delete(key)
