"""超预算只缩小失败批，不丢进度；同断点重试必须生成新的可投递消息。"""

import json
from datetime import UTC, datetime, timedelta
from time import monotonic

import pytest
from sqlalchemy import text
from sqlmodel import col, select

from app.jobs.models import PendingDispatch
from app.modules.reporting import health
from app.modules.reporting.store_models import AdSyncBc, ReportSyncHealth
from tests.modules.reporting.test_health_continuation import _case


def _pool(seed, factory, monkeypatch, redis_client):
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 64)
    with factory() as session, session.begin():
        session.execute(
            text("""
            INSERT INTO advertiser_account(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
            SELECT :tenant,'adaptive-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,126) i
        """),
            {"tenant": seed.tenant_id},
        )
        session.execute(
            text("""
            INSERT INTO ad_sync_account(tenant_id,advertiser_id,owner_bc_id)
            SELECT :tenant,'adaptive-'||i,'bc-a' FROM generate_series(1,126) i
        """),
            {"tenant": seed.tenant_id},
        )
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = health.owned_accounts(session, plan)
        session.add(
            ReportSyncHealth(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                metrics={"prior_complete": 1},
                last_repair_at=datetime.now(UTC),
            )
        )
    return args, health._health_progress_key(seed.tenant_id, "bc-a")


def _timeout(session, *_args, **_kwargs):
    # 本机PG取消信号可晚于100ms；保留真实50ms预算，用足够长的语句确保触发57014。
    with health.query_budget(session, seconds=0.05):
        session.execute(text("SELECT pg_sleep(1)"))


def test_same_offset_timeouts_shrink_and_publish_distinct_immediate_retries(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    original = health.measure_coverage
    monkeypatch.setattr(health, "measure_coverage", _timeout)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        cycle = json.loads(redis_client.get(key))["cycle"]
        # 模拟 broker 已接收分钟阶段登记的初始消息。
        with factory() as session, session.begin():
            for row in session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == health.HEALTH_BC_TASK,
                )
            ):
                row.published_at = datetime.now(UTC)
        original_at = None
        for expected_batch in [32, 16]:
            assert health.run_bc_health(**args, cycle=cycle) == "FAILED"
            progress = json.loads(redis_client.get(key))
            assert progress["offset"] == 0 and progress["metrics"] == {}
            assert progress["stages"]["directory"]["batch_size"] == expected_batch
            original_at = original_at or progress["at"]
            assert progress["at"] == original_at
            cycle = progress["cycle"]
            with factory() as session, session.begin():
                row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
                assert row.metrics["prior_complete"] == 1
                assert "missing_directory_ads" not in row.metrics
                assert "COVERAGE_QUERY_FAILED" in row.issues
                pending = session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == seed.tenant_id,
                        PendingDispatch.task_name == health.HEALTH_BC_TASK,
                        col(PendingDispatch.published_at).is_(None),
                    )
                ).all()
                assert len(pending) == 1 and pending[0].available_at <= datetime.now(
                    UTC
                )
                pending[0].published_at = datetime.now(UTC)
        with factory() as session:
            retries = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == health.HEALTH_BC_TASK,
                )
            ).all()
            assert len({row.task_key for row in retries}) == 3
        monkeypatch.setattr(health, "measure_coverage", original)
        sizes = []
        for _ in range(10):
            result = health.run_bc_health(**args, cycle=cycle)
            if result == "COMPLETE":
                break
            assert result == "CONTINUE"
            progress = json.loads(redis_client.get(key))
            sizes.append(progress["stages"]["directory"]["batch_size"])
        # 两次完整片取消已把增长上限定为16，不能被后续快批立即覆盖。
        assert result == "COMPLETE" and sizes and max(sizes) <= 16
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert (
                row.metrics["stale_ad_accounts"]
                == row.metrics["stale_material_accounts"]
                == 128
            )
            assert row.metrics["coverage_observed_at"] == original_at
            assert row.observed_at <= datetime.fromisoformat(original_at)
    finally:
        redis_client.delete(key)


def test_single_account_timeout_cools_down_without_skipping_or_retry_storm(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    original = health.measure_coverage
    calls = []

    def fail(session, *args, **kwargs):
        calls.append(True)
        return _timeout(session, *args, **kwargs)

    monkeypatch.setattr(health, "measure_coverage", fail)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        cycle = json.loads(redis_client.get(key))["cycle"]
        assert health.run_bc_health(**args, cycle=cycle) == "FAILED"
        progress = json.loads(redis_client.get(key))
        assert (
            progress["offset"] == 0
            and progress["stages"]["directory"]["batch_size"] == 1
        )
        assert datetime.fromisoformat(progress["retry_not_before"]) > datetime.now(
            UTC
        ) + timedelta(seconds=50)
        for cycle in [progress["cycle"], None, progress["cycle"]]:
            assert health.run_bc_health(**args, cycle=cycle) == "WAIT"
        assert len(calls) == 1
        with factory() as session:
            assert (
                len(
                    session.exec(
                        select(PendingDispatch).where(
                            PendingDispatch.tenant_id == seed.tenant_id,
                            PendingDispatch.task_name == health.HEALTH_BC_TASK,
                        )
                    ).all()
                )
                == 1
            )
        progress["retry_not_before"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        redis_client.set(key, json.dumps(progress), ex=180)
        monkeypatch.setattr(health, "measure_coverage", original)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "CONTINUE"
        assert json.loads(redis_client.get(key))["offset"] == 1
    finally:
        redis_client.delete(key)


def test_timeout_restores_savepoint_and_original_statement_timeout(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    assert health.run_bc_health(**args) == "CONTINUE"
    cycle = json.loads(redis_client.get(key))["cycle"]
    monkeypatch.setattr(health, "measure_coverage", _timeout)
    try:
        with factory() as session:
            session.execute(text("SET LOCAL statement_timeout='750ms'"))
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            with pytest.raises(health._HealthBatchTimeout):
                health._advance_health(
                    session, plan, redis_client, cycle=cycle, deadline=monotonic() + 10
                )
            assert session.execute(text("SELECT 1")).scalar_one() == 1
            assert (
                session.execute(text("SHOW statement_timeout")).scalar_one() == "750ms"
            )
    finally:
        redis_client.delete(key)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_legacy_progress_is_not_read_and_old_continuation_does_not_start_new_cycle(
    committed_sync_seed, redis_client, monkeypatch, version
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    legacy = key.replace(":v3:", f":{version}:")
    try:
        redis_client.set(
            legacy, json.dumps({"cycle": "old", "metrics": {"partial": 999}}), ex=180
        )
        assert health.run_bc_health(**args, cycle="old") == "IGNORED"
        assert redis_client.get(key) is None
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert progress["cycle"] != "old" and progress["offset"] == 0
        assert progress["metrics"] == {}
        assert health.run_bc_health(**args, cycle="old") == "IGNORED"
        assert json.loads(redis_client.get(key)) == progress
    finally:
        redis_client.delete(key, legacy)


def test_scope_change_during_failed_batch_does_not_enqueue_old_scope_retry(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    original = health.measure_coverage

    def change_scope_and_fail(session, *args, **kwargs):
        with factory() as writer, writer.begin():
            plan = writer.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            plan.frozen_route = plan.frozen_route | {"binding_revision": 99}
        return _timeout(session, *args, **kwargs)

    monkeypatch.setattr(health, "measure_coverage", change_scope_and_fail)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        cycle = json.loads(redis_client.get(key))["cycle"]
        assert health.run_bc_health(**args, cycle=cycle) == "FAILED"
        previous = json.loads(redis_client.get(key))
        with factory() as session:
            assert (
                len(
                    session.exec(
                        select(PendingDispatch).where(
                            PendingDispatch.tenant_id == seed.tenant_id,
                            PendingDispatch.task_name == health.HEALTH_BC_TASK,
                        )
                    ).all()
                )
                == 1
            )
        monkeypatch.setattr(health, "measure_coverage", original)
        assert health.run_bc_health(**args) == "CONTINUE"
        current = json.loads(redis_client.get(key))
        assert current["cycle"] != previous["cycle"]
        assert (
            current["stages"]["directory"]["batch_size"] == 64
            and current["offset"] == 0
        )
        assert health.run_bc_health(**args, cycle=previous["cycle"]) == "IGNORED"
    finally:
        redis_client.delete(key)
