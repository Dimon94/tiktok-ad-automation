"""覆盖阶段独立续点：真实PG取消不能迫使已完成目录重新读取。"""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from app.modules.reporting import health
from app.modules.reporting.store_models import (
    AdReportDay,
    AdSyncBc,
    MaterialReportDay,
    ReportSyncHealth,
)
from tests.modules.reporting.test_health_continuation import _case


def test_material_timeout_does_not_remeasure_successful_directory(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 64)
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    measured = Counter()
    original_directory = health._missing_directory_ads
    original_execute = Session.execute
    failures = []

    def directory(db, params):
        measured.update(params["ids"])
        return original_directory(db, params)

    def fail_material_once(db, statement, *args, **kwargs):
        if "WITH posts AS MATERIALIZED" in str(statement) and not failures:
            failures.append(True)
            with health.query_budget(db, seconds=0.05):
                original_execute(db, text("SELECT pg_sleep(1)"))
        return original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(health, "_missing_directory_ads", directory)
    monkeypatch.setattr(Session, "execute", fail_material_once)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        cycle = json.loads(redis_client.get(key))["cycle"]
        assert health.run_bc_health(**args, cycle=cycle) == "FAILED"
        saved = json.loads(redis_client.get(key))
        assert saved["stages"]["directory"]["offset"] == 2
        assert saved["stages"]["directory"]["batch_size"] == 64
        assert saved["stages"]["material"]["offset"] == 0
        assert saved["stages"]["material"]["batch_size"] == 1
        with factory() as db:
            row = db.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["coverage_status"] == "FAILED"
            assert "missing_directory_ads" not in row.metrics
        for _ in range(5):
            result = health.run_bc_health(**args, cycle=cycle)
            if result == "COMPLETE":
                break
            assert result == "CONTINUE"
        assert result == "COMPLETE"
        assert measured == {"acc-1": 1, "acc-2": 1}
    finally:
        redis_client.delete(key)


@pytest.mark.parametrize(
    "simulated_queue_seconds,expected", [(6, "COMPLETE"), (31, "FAILED")]
)
def test_4859_accounts_complete_or_expire_including_queue_time(
    committed_sync_seed,
    redis_client,
    monkeypatch,
    record_property,
    simulated_queue_seconds,
    expected,
):
    """真实4859户/9718事实/Redis续点；仅broker排队时钟模拟，不承诺线上速度。"""
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 64)
    monkeypatch.setattr(health, "HEALTH_MAX_CHUNKS", 16)
    delay = [0]

    class QueueClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=delay[0])

    monkeypatch.setattr(health, "datetime", QueueClock)
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        db.execute(
            text("""
            INSERT INTO advertiser_account(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
            SELECT :tenant,'scale-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,4857) i
        """),
            {"tenant": seed.tenant_id},
        )
        db.execute(
            text("""
            INSERT INTO ad_sync_account(tenant_id,advertiser_id,owner_bc_id)
            SELECT :tenant,'scale-'||i,'bc-a' FROM generate_series(1,4857) i
        """),
            {"tenant": seed.tenant_id},
        )
        db.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )
        plan = db.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        owned = health.owned_accounts(db, plan)
        assert len(owned) == 4859
        plan.last_active_advertiser_ids = owned
        for model, fields in [
            (AdReportDay, {"ad_id": "ad", "adgroup_id": "g", "campaign_id": "c"}),
            (
                MaterialReportDay,
                {"main_material_id": "post", "main_material_type": "VIDEO"},
            ),
        ]:
            db.execute(
                insert(model),
                [
                    {
                        "tenant_id": seed.tenant_id,
                        "advertiser_id": account,
                        "stat_date": now.date(),
                        "currency": "USD",
                        "timezone": "UTC",
                        "fetched_at": now,
                        "sync_job_id": uuid4(),
                        "spend": 1,
                        **fields,
                    }
                    for account in owned
                ],
            )
        db.add(
            ReportSyncHealth(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                metrics={"prior_complete": 1},
                last_repair_at=now,
            )
        )
    visited = {stage: Counter() for stage in health.COVERAGE_STAGES}
    original = health.measure_coverage

    def measure(*args, **kwargs):
        result = original(*args, **kwargs)
        visited[kwargs["stage"]].update(kwargs["accounts"])
        return result

    monkeypatch.setattr(health, "measure_coverage", measure)
    started = monotonic()
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        for invocation in range(1, 10):
            assert invocation <= 6
            delay[0] += simulated_queue_seconds
            result = health.run_bc_health(**args, cycle=progress["cycle"])
            if result != "CONTINUE":
                break
            saved = json.loads(redis_client.get(key))
            assert saved["at"] == progress["at"]
            with factory() as db:
                metrics = db.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics
                assert "missing_directory_ads" not in metrics
        assert result == expected and redis_client.get(key) is None
        with factory() as db:
            metrics = db.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics
            if expected == "COMPLETE":
                assert metrics["missing_directory_ads"] == 4859
                assert metrics["unlinked_material_posts"] == 4859
                assert metrics["coverage_observed_at"] == progress["at"]
                assert invocation == 5  # 16 complete blocks/entry remains intact.
                for stage in health.COVERAGE_STAGES:
                    assert visited[stage] == dict.fromkeys(owned, 1)
            else:
                assert metrics["coverage_status"] == "FAILED"
                assert "missing_directory_ads" not in metrics
                assert delay[0] >= 120
        record_property("actual_local_seconds", round(monotonic() - started, 3))
        record_property("simulated_broker_wait_seconds", delay[0])
        record_property("coverage_invocations", invocation)
    finally:
        redis_client.delete(key, health._health_batch_hint_key(seed.tenant_id, "bc-a"))


@pytest.mark.parametrize("invalidate", ["scope", "deadline"])
def test_completed_stages_revalidate_scope_and_deadline_before_publish(
    committed_sync_seed, redis_client, monkeypatch, invalidate
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 64)
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    original = health.measure_coverage
    delay = [0]

    class QueueClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=delay[0])

    monkeypatch.setattr(health, "datetime", QueueClock)

    def measure(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["stage"] == "material":
            if invalidate == "scope":
                with factory() as writer, writer.begin():
                    plan = writer.get(AdSyncBc, (seed.tenant_id, "bc-a"))
                    plan.frozen_route = plan.frozen_route | {"binding_revision": 99}
            else:
                delay[0] = 121
        return result

    monkeypatch.setattr(health, "measure_coverage", measure)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == (
            "IGNORED" if invalidate == "scope" else "FAILED"
        )
        assert redis_client.get(key) is None
        with factory() as db:
            assert (
                "missing_directory_ads"
                not in db.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics
            )
    finally:
        redis_client.delete(key)


def test_only_same_scope_batch_sizes_survive_a_new_cycle(redis_client):
    progress = health._new_coverage_progress("scope-a")
    progress["stages"]["directory"]["batch_size"] = 8
    progress["stages"]["directory"]["offset"] = 64
    progress["metrics"] = {"missing_directory_ads": 99}
    progress["repairs"]["ad"] = ["old-account"]
    from types import SimpleNamespace

    plan = SimpleNamespace(tenant_id=uuid4(), bc_id="bc-a")
    key = health._health_batch_hint_key(plan.tenant_id, plan.bc_id)
    try:
        health._save_coverage_batch_hint(redis_client, plan, progress)
        hints = json.loads(redis_client.get(key))
        assert set(hints) == {"scope", "sizes"}
        assert 0 < redis_client.ttl(key) <= 600
        for scope, size in [("scope-a", 8), ("scope-b", 64)]:
            new = health._new_coverage_progress(scope, hints)
            assert new["cycle"] != progress["cycle"]
            assert new["at"] != progress["at"]
            assert new["metrics"] == {} and new["repairs"]["ad"] == []
            assert new["stages"]["directory"]["batch_size"] == size
            assert all(part["offset"] == 0 for part in new["stages"].values())
    finally:
        redis_client.delete(key)


def test_stage_reserve_yields_without_reducing_the_query_budget(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    original = health.measure_coverage
    budgets = []

    def measure(db, *args, **kwargs):
        budgets.append(
            db.execute(text("SELECT current_setting('statement_timeout')::interval"))
            .scalar_one()
            .total_seconds()
        )
        return original(db, *args, **kwargs)

    monkeypatch.setattr(health, "measure_coverage", measure)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        with factory() as db, db.begin():
            plan = db.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            assert (
                health._advance_health(
                    db,
                    plan,
                    redis_client,
                    cycle=progress["cycle"],
                    deadline=monotonic() + 0.9,
                )
                == "CONTINUE"
            )
        assert budgets == []
        assert json.loads(redis_client.get(key))["offset"] == 0
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "CONTINUE"
        assert len(budgets) == 2 and all(0 < seconds <= 3 for seconds in budgets)
    finally:
        redis_client.delete(key)
