"""跨任务完整观测：进度不是健康指标，失败分片不能跳过。"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core.context import TenantContext
from app.modules.reporting import health, planner
from app.modules.reporting.store_models import AdSyncBc, ReportSyncHealth


def _case(seed, factory, monkeypatch):
    monkeypatch.setattr(health.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 1)
    monkeypatch.setattr(health, "HEALTH_MAX_CHUNKS", 1)
    with factory() as session:
        planner.ensure_bc_plans(session, now=datetime.now(UTC))
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.active_screened_at = datetime.now(UTC)
        plan.last_active_advertiser_ids = ["acc-2"]
        session.commit()
    return {
        "database_engine": factory().bind,
        "context": TenantContext(seed.tenant_id, seed.actor_id, "operator"),
        "bc_id": "bc-a",
    }


def _start_coverage(args, redis_client, key):
    assert health.run_bc_health(**args) == "CONTINUE"
    return json.loads(redis_client.get(key))["cycle"]


def test_second_chunk_failure_resumes_without_partial_success(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert progress["offset"] == 0
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert progress["offset"] == 1
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            fresh_at = row.observed_at
            assert row.metrics["coverage_status"] == "PENDING"
            assert "missing_directory_ads" not in row.metrics
        original = health.measure_coverage

        def failed(session, *_args, **_kwargs):
            with health.query_budget(session, seconds=0.05):
                session.execute(text("SELECT pg_sleep(1)"))

        monkeypatch.setattr(health, "measure_coverage", failed)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "FAILED"
        assert json.loads(redis_client.get(key))["offset"] == 1
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert "COVERAGE_QUERY_FAILED" in row.issues
            assert "missing_directory_ads" not in row.metrics
        monkeypatch.setattr(health, "measure_coverage", original)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "WAIT"
        resumed = json.loads(redis_client.get(key))
        resumed["retry_not_before"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        redis_client.set(key, json.dumps(resumed), ex=180)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "COMPLETE"
        assert redis_client.get(key) is None
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["stale_ad_accounts"] == 1
            assert row.metrics["stale_material_accounts"] == 1
            assert row.observed_at == fresh_at
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "IGNORED"
    finally:
        redis_client.delete(key)


def test_scope_change_resets_cycle_and_old_continuation_cannot_advance(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        previous = json.loads(redis_client.get(key))
        with factory() as session:
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            plan.frozen_route = plan.frozen_route | {"binding_revision": 99}
            session.commit()
        assert health.run_bc_health(**args) == "CONTINUE"
        current = json.loads(redis_client.get(key))
        assert current["cycle"] != previous["cycle"] and current["offset"] == 0
        assert health.run_bc_health(**args, cycle=previous["cycle"]) == "IGNORED"
        assert json.loads(redis_client.get(key)) == current
    finally:
        redis_client.delete(key)


def test_expired_cycle_does_not_publish_old_healthy_measurement(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        progress["at"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
        redis_client.set(key, json.dumps(progress), ex=180)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "FAILED"
        assert redis_client.get(key) is None
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert "COVERAGE_QUERY_FAILED" in row.issues
            assert "missing_directory_ads" not in row.metrics
    finally:
        redis_client.delete(key)


def test_bc_level_pending_actions_are_not_added_for_each_chunk(
    committed_sync_seed, redis_client, monkeypatch
):
    from app.modules.ad_management.models import ManagementPreview, ManagementTask
    from app.modules.reporting.query_models import FrozenSelectionRecord

    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    with factory() as session:
        scope: dict[str, Any] = {
            "tenant_id": seed.tenant_id,
            "bc_id": "bc-a",
            "actor_id": seed.actor_id,
        }
        selection = FrozenSelectionRecord(
            **scope,
            snapshot_id=uuid4(),
            filter_digest="a" * 64,
            membership_digest="b" * 64,
        )
        session.add(selection)
        session.flush()
        preview = ManagementPreview(**scope, selection_id=selection.id, digest="c" * 64)
        session.add(preview)
        session.flush()
        session.add(
            ManagementTask(
                **scope,
                preview_id=preview.id,
                idempotency_key=uuid4(),
                digest="d" * 64,
                status="QUEUED",
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        session.commit()
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert "pending_actions" not in progress["metrics"]
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "CONTINUE"
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "COMPLETE"
        with factory() as session:
            assert (
                session.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics[
                    "pending_actions"
                ]
                == 1
            )
    finally:
        redis_client.delete(key)


def test_scope_fingerprint_covers_actor_and_owned_but_not_screen_progress(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    _case(seed, factory, monkeypatch)
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        original = health._health_scope(session, plan, ["acc-1", "acc-2"])
        assert health._health_scope(session, plan, ["acc-2", "acc-1"]) == original
        assert health._health_scope(session, plan, ["acc-1"]) != original
        plan.actor_id = uuid4()
        assert health._health_scope(session, plan, ["acc-1", "acc-2"]) != original
        plan.actor_id = seed.actor_id
        plan.last_active_advertiser_ids = ["acc-1", "acc-2"]
        plan.active_screened_at = datetime.now(UTC)
        assert health._health_scope(session, plan, ["acc-1", "acc-2"]) == original


def test_thousand_accounts_complete_across_tasks_without_partial_metrics(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 64)
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    with factory() as session:
        session.execute(
            text("""
            INSERT INTO advertiser_account(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
            SELECT :tenant,'scale-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,1000) i
        """),
            {"tenant": seed.tenant_id},
        )
        session.execute(
            text("""
            INSERT INTO ad_sync_account(tenant_id,advertiser_id,owner_bc_id)
            SELECT :tenant,'scale-'||i,'bc-a' FROM generate_series(1,1000) i
        """),
            {"tenant": seed.tenant_id},
        )
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = ["acc-2"] + [
            f"scale-{i}" for i in range(1, 1001)
        ]
        session.add(
            ReportSyncHealth(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                metrics={"prior_complete": 1},
                last_repair_at=datetime.now(UTC),
            )
        )
        session.commit()
    try:
        cycle = None
        calls = 0
        while True:
            result = health.run_bc_health(**args, cycle=cycle)
            calls += 1
            assert calls <= 17
            with factory() as session:
                row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
                if result == "COMPLETE":
                    assert row.metrics["stale_ad_accounts"] == 1001
                    assert row.metrics["stale_material_accounts"] == 1001
                    break
                assert result == "CONTINUE" and row.metrics["prior_complete"] == 1
                assert "missing_directory_ads" not in row.metrics
                assert row.metrics["stale_ad_accounts"] == 1001
            progress = json.loads(redis_client.get(key))
            cycle = progress["cycle"]
        assert calls == 17 and redis_client.get(key) is None
    finally:
        redis_client.delete(key)
