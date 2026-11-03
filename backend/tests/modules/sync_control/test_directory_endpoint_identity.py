"""目录的候补、成本与实际SDK必须使用同一个物理接口。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlmodel import select

from app.integrations.tiktok.platform_quotas import sdk_endpoint
from app.modules.sync_control.directory_demand import directory_endpoint
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_creative_primary_budget_matches_real_ad_operation(session, sync_seed):
    from app.integrations.tiktok.adapters.ads_read import compile_query
    from app.integrations.tiktok.contracts.ads import DirectoryQuery

    run = _directory(session, sync_seed)
    run.kind = "creative"
    run.query = {
        "advertiser_id": "acc-1",
        "kind": "creative",
        "ad_type": "SMART_PLUS",
        "page": 1,
        "page_size": 100,
        "ids": [],
        "parent_ids": [],
        "include_deleted": True,
    }
    operation, _ = compile_query(
        DirectoryQuery(**{**run.query, "ids": (), "parent_ids": ()})
    )
    assert operation == "ads.get_ads"
    assert directory_endpoint(run) == sdk_endpoint(operation)


def test_stale_head_is_rejected_before_any_fair_budget_or_reservation(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.reporting.store_models import ReportDispatchCursor
    from app.modules.sync_control.dispatch_budget import EndpointBudgets

    run = _directory(session, sync_seed)
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    head.physical_endpoint = "GET /wrong/stale/endpoint/"
    session.flush()
    acquired = []
    original = EndpointBudgets.acquire

    def acquire(self, tenant, endpoint):
        acquired.append(endpoint)
        return original(self, tenant, endpoint)

    monkeypatch.setattr(EndpointBudgets, "acquire", acquire)
    assert (
        dispatch_directories_due(session, redis_client, now=datetime.now(UTC), limit=1)
        == 0
    )
    assert acquired == []
    assert session.get(ReportDispatchCursor, sync_seed.tenant_id) is None
    assert not session.exec(
        select(SyncCostLedger).where(SyncCostLedger.directory_run_id == run.id)
    ).all()
    session.refresh(run)
    assert run.claim_generation == 1 and run.claim_token is None
    assert head.physical_endpoint == "GET /wrong/stale/endpoint/"


def test_endpoint_repair_preserves_fences_and_skips_live_grants_and_claims(
    session, sync_seed
):
    from datetime import timedelta

    from app.modules.sync_control.directory_endpoint_repair import (
        repair_directory_endpoint_batch,
    )
    from app.modules.sync_control.models import SyncScope

    runs = [
        _directory(session, sync_seed, query_extra={"page_size": i + 1})
        for i in range(5)
    ]
    heads = []
    for i, run in enumerate(runs):
        from app.modules.sync_control.models import SyncExecutionScope

        child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.directory_run_id == run.id
            )
        ).one()
        head = session.get(SyncSchedule, (child.scope_id, "directory"))
        scope = session.get(SyncScope, head.scope_id)
        scope.resource = (
            "directory:creative:SMART_PLUS" if i != 4 else "directory:ad:SMART_PLUS"
        )
        head.physical_endpoint = sdk_endpoint("build.get_ads")
        head.active = i != 1
        heads.append(head)
    assert len({h.scope_id for h in heads}) == 5
    now = datetime.now(UTC)
    heads[2].leased_until = now + timedelta(minutes=4)
    heads[2].lease_token = uuid4()
    heads[2].lease_execution_id, heads[2].lease_execution_generation = runs[2].id, 1
    runs[3].claimed_until = now + timedelta(seconds=90)
    heads.append(
        SyncSchedule(
            scope_id=heads[0].scope_id,
            tenant_id=sync_seed.tenant_id,
            work_class="realtime",
            next_eligible_at=now,
            physical_endpoint=sdk_endpoint("build.get_ads"),
            active=False,
        )
    )
    session.add(heads[-1])
    session.flush()
    before = {
        (h.scope_id, h.work_class): h.model_dump(exclude={"physical_endpoint"})
        for h in heads
    }
    preview = repair_directory_endpoint_batch(session, now=now)
    assert set(preview) == {
        (heads[0].scope_id, "directory"),
        (heads[1].scope_id, "directory"),
        (heads[0].scope_id, "realtime"),
    }
    assert all(h.physical_endpoint == sdk_endpoint("build.get_ads") for h in heads)
    import pytest

    with pytest.raises(ValueError, match="old dispatchers"):
        repair_directory_endpoint_batch(session, now=now, apply=True)
    changed = repair_directory_endpoint_batch(
        session, now=now, apply=True, old_dispatchers_stopped=True
    )
    assert set(changed) == set(preview)
    for i, head in enumerate(heads):
        session.refresh(head)
        assert (
            head.model_dump(exclude={"physical_endpoint"})
            == before[(head.scope_id, head.work_class)]
        )
        assert head.physical_endpoint == sdk_endpoint(
            "ads.get_ads" if i < 2 or head.work_class == "realtime" else "build.get_ads"
        )
    assert (
        repair_directory_endpoint_batch(
            session, now=now, apply=True, old_dispatchers_stopped=True
        )
        == []
    )
    assert (
        len(repair_directory_endpoint_batch(session, now=now + timedelta(minutes=5)))
        == 2
    )


def test_repair_skips_locked_heads_and_keyset_resumes_before_final_sweep(
    committed_sync_seed,
):
    from datetime import timedelta
    from time import monotonic

    from app.modules.sync_control.directory_endpoint_repair import (
        repair_directory_endpoint_batch,
    )
    from app.modules.sync_control.models import SyncExecutionScope, SyncScope

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        keys = []
        for i in range(3):
            run = _directory(db, seed, query_extra={"page_size": i + 1})
            child = db.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.directory_run_id == run.id
                )
            ).one()
            db.get(SyncScope, child.scope_id).resource = "directory:creative:SMART_PLUS"
            head = db.get(SyncSchedule, (child.scope_id, "directory"))
            head.physical_endpoint = sdk_endpoint("build.get_ads")
            keys.append((head.scope_id, head.work_class))
        keys.sort()
    now = datetime.now(UTC) + timedelta(seconds=1)
    with factory() as blocker, blocker.begin():
        blocker.get(SyncSchedule, keys[0], with_for_update=True)
        started = monotonic()
        for expected, after in [(keys[1], None), (keys[2], keys[1])]:
            with factory() as db, db.begin():
                assert repair_directory_endpoint_batch(
                    db,
                    now=now,
                    limit=1,
                    after=after,
                    apply=True,
                    old_dispatchers_stopped=True,
                ) == [expected]
        assert monotonic() - started < 2
        with factory() as db:
            assert db.get(SyncSchedule, keys[0]).physical_endpoint == sdk_endpoint(
                "build.get_ads"
            )
    with factory() as db, db.begin():
        assert (
            repair_directory_endpoint_batch(
                db, now=now, after=keys[2], apply=True, old_dispatchers_stopped=True
            )
            == []
        )
        # 跳锁行不丢弃；下一次从头扫描完成它，重复执行为空。
        assert repair_directory_endpoint_batch(
            db, now=now, apply=True, old_dispatchers_stopped=True
        ) == [keys[0]]
        assert (
            repair_directory_endpoint_batch(
                db, now=now, apply=True, old_dispatchers_stopped=True
            )
            == []
        )


def test_endpoint_change_after_discovery_cannot_charge_another_budget(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.sync_control.dispatch_budget import EndpointBudgets

    run = _directory(session, sync_seed)
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    original = EndpointBudgets.acquire

    def change_after_discovery(self, tenant, endpoint):
        budget = original(self, tenant, endpoint)
        # 模拟发现后另一写入者改变数据库行；保留发现时ORM快照直到短锁复查。
        from sqlalchemy import text

        session.connection().execute(
            text(
                "UPDATE sync_schedule SET physical_endpoint=:ep WHERE scope_id=:id AND work_class=:wc"
            ),
            {
                "ep": "GET /changed/after-discovery/",
                "id": head.scope_id,
                "wc": head.work_class,
            },
        )
        return budget

    monkeypatch.setattr(EndpointBudgets, "acquire", change_after_discovery)
    assert (
        dispatch_directories_due(session, redis_client, now=datetime.now(UTC), limit=1)
        == 0
    )
    session.refresh(run)
    assert run.claim_generation == 1
    assert not session.exec(
        select(SyncCostLedger).where(SyncCostLedger.directory_run_id == run.id)
    ).all()
