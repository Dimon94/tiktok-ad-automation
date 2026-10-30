"""公共物理池不能把另一个已被平台限流接口的积压当成可执行赢家。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import quota_configuration
from app.jobs.admission import admission_policy, admit_call
from app.modules.reporting.jobs import enqueue_job
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.dispatcher import dispatch_due
from tests.modules.reporting.test_directory_direct_delivery import directory
from tests.modules.reporting.test_jobs import _route


def test_multiple_quota_blocked_fact_heads_cannot_hold_idle_directory_pool(
    session,
    sync_seed,
    redis_client,
    monkeypatch,
):
    from app.modules.reporting import job_tasks  # noqa:F401

    now = datetime.now(UTC)
    endpoint = "GET /open_api/v1.3/report/integrated/get/"
    policy = quota_configuration("BASIC")
    policy["endpoints"]["OFFICIAL_API:" + endpoint] = {"endpoint_calls_per_day": 1}
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", policy)
    for i in range(3):
        day = now.date() - timedelta(days=5 + i)
        enqueue_job(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            kind="ad_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=day,
            end=day,
            campaign_ids=[str(i)],
            frozen_route=_route(sync_seed),
            due_at=now,
        )
    run = directory(session, sync_seed)
    run.workload = "background"
    register_directory_run(session, run, new_execution=True)
    assert admit_call(
        redis_client,
        lease_id=uuid4(),
        policy=admission_policy(
            "reports.integrated", channel="OFFICIAL_API", physical_endpoint=endpoint
        ),
        app_scope=settings.TIKTOK_APP_ID,
        endpoint=endpoint,
        rate_group=endpoint,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
    ).granted
    assert (
        dispatch_due(
            session, redis_client, now=now + timedelta(seconds=1), physical_slots=36
        )
        == 0
    )
    assert (
        dispatch_directories_due(
            session, redis_client, now=now + timedelta(seconds=1), limit=40
        )
        == 1
    )


def test_readiness_cursor_reaches_ready_material_after_sixty_five_blocked_fact_scopes(
    session,
    sync_seed,
    redis_client,
    monkeypatch,
):
    from uuid import UUID

    from sqlalchemy import insert
    from sqlmodel import select

    from app.modules.reporting.store_models import ReportSyncJob
    from app.modules.sync_control.models import (
        SyncExecutionScope,
        SyncRequirement,
        SyncSchedule,
        SyncScope,
        SyncWorkerPool,
    )
    from app.modules.sync_control.worker_pool import _ready_categories

    now = datetime.now(UTC)
    endpoint = "GET /open_api/v1.3/report/integrated/get/"
    config = quota_configuration("BASIC")
    config["endpoints"]["OFFICIAL_API:" + endpoint] = {"endpoint_calls_per_day": 1}
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", config)
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": now.date() - timedelta(days=5),
        "end": now.date() - timedelta(days=5),
        "frozen_route": _route(sync_seed),
        "due_at": now,
    }
    original = enqueue_job(session, kind="ad_day", **common)
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == original)
    ).one()
    models = (
        SyncScope,
        SyncRequirement,
        ReportSyncJob,
        SyncExecutionScope,
        SyncSchedule,
    )
    templates = (
        session.get(SyncScope, child.scope_id).model_dump(),
        session.get(SyncRequirement, child.requirement_id).model_dump(),
        session.get(ReportSyncJob, original).model_dump(),
        child.model_dump(),
        session.get(SyncSchedule, (child.scope_id, "history")).model_dump(),
    )
    batches = [[] for _ in models]
    for i in range(1, 66):
        scope, requirement, job = UUID(int=i), uuid4(), uuid4()
        rows = [dict(template) for template in templates]
        rows[0].update(
            id=scope,
            identity_digest=f"{i:064x}",
            query_contract={"campaign_ids": [str(i)]},
        )
        rows[1].update(id=requirement, scope_id=scope)
        rows[2].update(id=job, job_key=str(job), campaign_ids=[str(i)])
        rows[3].update(
            id=uuid4(), scope_id=scope, requirement_id=requirement, report_job_id=job
        )
        rows[4].update(scope_id=scope)
        for batch, row in zip(batches, rows, strict=True):
            batch.append(row)
    for model, rows in zip(models, batches, strict=True):
        session.connection().execute(insert(model), rows)
    enqueue_job(session, kind="material_day", **common)
    assert admit_call(
        redis_client,
        lease_id=uuid4(),
        policy=admission_policy(
            "reports.integrated", channel="OFFICIAL_API", physical_endpoint=endpoint
        ),
        app_scope=settings.TIKTOK_APP_ID,
        endpoint=endpoint,
        rate_group=endpoint,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
    ).granted
    pool = session.get(SyncWorkerPool, "background")
    if pool is None:
        pool = SyncWorkerPool(
            pool_name="background", fairness_state={}, readiness_cursor={}
        )
        session.add(pool)
    pool.readiness_cursor = {}
    first = _ready_categories(
        session,
        redis_client,
        pool=pool,
        category="directory",
        now=now,
        allow_backfill=True,
        budget_seconds=1,
        probe_limit=64,
    )
    assert first == {"directory"}
    assert pool.readiness_cursor["facts"] == str(UUID(int=64))
    second = _ready_categories(
        session,
        redis_client,
        pool=pool,
        category="directory",
        now=now,
        allow_backfill=True,
        budget_seconds=1,
        probe_limit=64,
    )
    assert second == {"directory", "facts"}


def test_dispatch_readiness_observes_queued_capacity_without_writing_keys(redis_client):
    from app.jobs.admission import AdmissionPolicy, admission_keys
    from app.modules.sync_control.occupancy import (
        dispatch_readiness,
        occupancy_keys,
        reserve_dispatch,
    )

    scope = {
        "app_scope": f"observe-pool-{uuid4()}",
        "endpoint": "GET /example/",
        "rate_group": "GET /example/",
        "tenant_id": uuid4(),
        "advertiser_id": "test-account",
    }
    policy = AdmissionPolicy(
        app_max_inflight=2,
        endpoint_max_inflight=2,
        tenant_max_inflight=2,
        advertiser_max_inflight=2,
        app_calls_per_window=2,
        endpoint_calls_per_window=2,
        window_ms=1000,
        lease_ms=1000,
    )
    reserve_dispatch(redis_client, reservation_id=uuid4(), policy=policy, **scope)
    keys = admission_keys(**scope) + occupancy_keys(**scope)
    before = [redis_client.dump(key) for key in keys]
    for _ in range(10):
        assert (
            dispatch_readiness(redis_client, policy=policy, **scope).capacity_hint == 1
        )
    assert before == [redis_client.dump(key) for key in keys]
