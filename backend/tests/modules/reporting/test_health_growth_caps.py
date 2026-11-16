"""同范围失败经验限增长；真实PG取消/Redis续点，不跨周期复用测量结果。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.modules.reporting import health
from app.modules.reporting.store_models import ReportSyncHealth
from tests.modules.reporting.test_health_adaptive_batches import _pool
from tests.modules.reporting.test_health_tail_budget import _short_entry


def _start(args, key, redis_client):
    assert health.run_bc_health(**args) == "CONTINUE"
    return json.loads(redis_client.get(key))


def _finish(args, key, progress, redis_client):
    for _ in range(16):
        result = health.run_bc_health(**args, cycle=progress["cycle"])
        if result == "COMPLETE":
            assert redis_client.get(key) is None
            return
        assert result in {"CONTINUE", "FAILED"}
    pytest.fail("128-account coverage did not finish with bounded continuation")


def test_real_full_timeouts_do_not_regrow_into_repeated_dense_batches_or_next_cycle(
    committed_sync_seed, redis_client, monkeypatch
):
    """人为注入64户批的真实57014，只证明防抖与提交语义，不代表生产容量。"""
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    hint_key = health._health_batch_hint_key(seed.tenant_id, "bc-a")
    original = health.measure_coverage
    cancelled, measured = [], []

    def dense(db, *args, **kwargs):
        size = len(kwargs["accounts"])
        measured.append((kwargs["stage"], size))
        if kwargs["stage"] == "directory" and size == 64:
            try:
                db.execute(text("SELECT pg_sleep(3.1)"))
            except DBAPIError as error:
                cancelled.append(getattr(error.orig, "sqlstate", None))
                raise
        return original(db, *args, **kwargs)

    monkeypatch.setattr(health, "measure_coverage", dense)
    try:
        first = _start(args, key, redis_client)
        _finish(args, key, first, redis_client)
        # 原逻辑：两个32快成功立刻升回64，在本轮第二片再取消。
        assert cancelled == ["57014"]
        hints = json.loads(redis_client.get(hint_key))
        cap = hints["growth_caps"]["directory"]
        assert cap["growth_cap"] == 32
        assert datetime.now(UTC) < datetime.fromisoformat(cap["cap_until"])
        assert all(size <= 32 for stage, size in measured[1:] if stage == "directory")
        with factory() as db, db.begin():
            row = db.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["coverage_observed_at"] == first["at"]
            row.metrics = row.metrics | {
                "coverage_observed_at": (
                    datetime.now(UTC) - timedelta(minutes=4)
                ).isoformat()
            }
        second = _start(args, key, redis_client)
        assert second["cycle"] != first["cycle"] and second["at"] != first["at"]
        assert second["metrics"] == {} and second["repairs"] == {
            name: [] for name in ("ad", "material", "directory", *health.PARENT_KINDS)
        }
        assert all(stage["offset"] == 0 for stage in second["stages"].values())
        assert second["stages"]["directory"]["growth_cap"] == 32
        assert second["stages"]["directory"]["cap_until"] == cap["cap_until"]
        _finish(args, key, second, redis_client)
        assert cancelled == ["57014"]
        saved = json.loads(redis_client.get(hint_key))
        assert saved["growth_caps"]["directory"] == cap
    finally:
        redis_client.delete(key, hint_key)


@pytest.mark.parametrize(
    "change", ["same", "expired", "expires_during_cycle", "scope", "legacy"]
)
def test_hint_reuse_requires_scope_and_absolute_unexpired_cap(
    committed_sync_seed, redis_client, monkeypatch, change
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    hint_key = health._health_batch_hint_key(seed.tenant_id, "bc-a")
    try:
        old = _start(args, key, redis_client)
        cap_until = (
            datetime.now(UTC)
            + timedelta(seconds=5 if change == "expires_during_cycle" else 600)
        ).isoformat()
        old["stages"]["directory"].update(
            batch_size=16, growth_cap=32, cap_until=cap_until
        )
        old["metrics"] = {"missing_directory_ads": 999}
        old["repairs"]["ad"] = ["old-account"]
        with factory() as db:
            plan = db.get(health.AdSyncBc, (seed.tenant_id, "bc-a"))
            health._save_coverage_batch_hint(redis_client, plan, old)
        hints = json.loads(redis_client.get(hint_key))
        if change == "legacy":
            hints.pop("growth_caps", None)
            redis_client.set(hint_key, json.dumps(hints), ex=600)
        elif change == "expired":
            # 不等待10分钟；只推进应用UTC时钟，仍使用真实PG/Redis。
            class ExpiredClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(seconds=601)

            monkeypatch.setattr(health, "datetime", ExpiredClock)
        elif change == "scope":
            with factory() as db, db.begin():
                db.execute(
                    text(
                        "UPDATE advertiser_account SET timezone='Europe/Paris' WHERE tenant_id=:tenant"
                    ),
                    {"tenant": seed.tenant_id},
                )
        elif change == "expires_during_cycle":
            clock_shift = [0]

            class AdvancingClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(seconds=clock_shift[0])

            monkeypatch.setattr(health, "datetime", AdvancingClock)
        redis_client.delete(key)
        new = _start(args, key, redis_client)
        assert new["metrics"] == {} and new["repairs"]["ad"] == []
        assert all(stage["offset"] == 0 for stage in new["stages"].values())
        stage = new["stages"]["directory"]
        active_cap = change in {"same", "expires_during_cycle"}
        assert stage.get("growth_cap", 64) == (32 if active_cap else 64)
        assert stage.get("cap_until") == (cap_until if active_cap else None)
        if change == "scope":
            assert new["scope"] != old["scope"] and stage["batch_size"] == 64
        else:
            assert stage["batch_size"] == 16
        if active_cap:
            # 成功改变size并写hint，不能顺便把失败经验的期限续到未来。
            assert health.run_bc_health(**args, cycle=new["cycle"]) == "CONTINUE"
            assert health.run_bc_health(**args, cycle=new["cycle"]) == "CONTINUE"
            saved = json.loads(redis_client.get(hint_key))
            assert saved["growth_caps"]["directory"]["cap_until"] == cap_until
            if change == "expires_during_cycle":
                clock_shift[0] = 6
                assert health.run_bc_health(**args, cycle=new["cycle"]) == "CONTINUE"
                saved = json.loads(redis_client.get(key))
                assert saved["stages"]["directory"]["batch_size"] == 64
                hints = json.loads(redis_client.get(hint_key))
                assert "directory" not in hints.get("growth_caps", {})
        elif change in {"expired", "legacy"}:
            # 到期/旧v3没有cap后，仍能从16经过连续快批恢复64，不永久降吞吐。
            for _ in range(3):
                assert health.run_bc_health(**args, cycle=new["cycle"]) == "CONTINUE"
            saved = json.loads(redis_client.get(key))
            assert saved["stages"]["directory"]["batch_size"] == 64
    finally:
        redis_client.delete(key, hint_key)


def test_real_tail_cancel_keeps_existing_cap_and_absolute_expiry(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    hint_key = health._health_batch_hint_key(seed.tenant_id, "bc-a")
    progress = _start(args, key, redis_client)
    cap_until = (datetime.now(UTC) + timedelta(seconds=600)).isoformat()
    progress["stages"]["directory"].update(
        batch_size=32, growth_cap=32, cap_until=cap_until
    )
    redis_client.set(key, json.dumps(progress), ex=180)
    with factory() as db:
        plan = db.get(health.AdSyncBc, (seed.tenant_id, "bc-a"))
        health._save_coverage_batch_hint(redis_client, plan, progress)
    prior_hint = redis_client.get(hint_key)
    _short_entry(monkeypatch)
    cancelled = []

    def timeout(db, *_args, **_kwargs):
        try:
            db.execute(text("SELECT pg_sleep(2)"))
        except DBAPIError as error:
            cancelled.append(getattr(error.orig, "sqlstate", None))
            raise

    monkeypatch.setattr(health, "measure_coverage", timeout)
    try:
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "CONTINUE"
        assert cancelled == ["57014"]
        saved = json.loads(redis_client.get(key))
        assert saved["stages"] == progress["stages"]
        assert saved["cycle"] == progress["cycle"] and saved["at"] == progress["at"]
        assert saved["metrics"] == {} and saved["retry_not_before"] is None
        assert redis_client.get(hint_key) == prior_hint
    finally:
        redis_client.delete(key, hint_key)
