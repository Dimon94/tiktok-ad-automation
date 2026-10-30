from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlmodel import select

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import quota_configuration
from app.jobs.models import PendingDispatch
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.models import SyncSchedule
from tests.modules.reporting.test_jobs import _route


def test_full_endpoint_keeps_demands_in_db_instead_of_broker_retries(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.reporting import job_tasks  # noqa: F401

    policy = quota_configuration("BASIC")
    endpoint = "GET /open_api/v1.3/report/integrated/get/"
    policy["endpoints"]["OFFICIAL_API:" + endpoint] = {"endpoint_calls_per_window": 1}
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", policy)
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"test-dispatch-{uuid4()}")
    now = datetime.now(UTC)
    for index in range(20):
        day = date(2026, 9, 1) + timedelta(days=index)
        enqueue_job(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=day,
            end=day,
            frozen_route=_route(sync_seed),
            actor_id=sync_seed.actor_id,
            due_at=now,
        )
    assert dispatch_due(session, redis_client, now=now, physical_slots=10) == 1
    assert dispatch_due(session, redis_client, now=now, physical_slots=10) == 0
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == sync_seed.tenant_id
                )
            ).all()
        )
        == 1
    )
    heads = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(heads) == 1
    assert heads[0].active


def test_single_worker_slot_rotates_between_ready_physical_endpoints(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.reporting import job_tasks  # noqa: F401
    from app.modules.sync_control.schedule import record_report_outcome

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"endpoint-turn-{uuid4()}")
    now = datetime.now(UTC)
    for kind in ("ad_day", "material_day"):
        for offset in range(2):
            day = date(2026, 9, 1) + timedelta(days=offset)
            enqueue_job(
                session,
                tenant_id=sync_seed.tenant_id,
                bc_id="bc-a",
                kind=kind,
                tier="history",
                advertiser_ids=["acc-1"],
                start=day,
                end=day,
                frozen_route=_route(sync_seed),
                actor_id=sync_seed.actor_id,
                due_at=now,
            )
    kinds = []
    for turn in range(2):
        assert (
            dispatch_due(
                session,
                redis_client,
                now=now + timedelta(seconds=turn),
                physical_slots=1,
            )
            == 1
        )
        claimed = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == sync_seed.tenant_id,
                ReportSyncJob.status == "RUNNING",
            )
        ).one()
        kinds.append(claimed.kind)
        claimed.status = "COMPLETE"
        record_report_outcome(session, claimed)
        claimed.claim_token = claimed.claimed_until = None
        session.flush()
    assert set(kinds) == {"ad_day", "material_day"}


def test_claimed_authorization_failure_retires_matching_child_generation(
    session, sync_seed, redis_client
):
    from app.modules.reporting import job_tasks  # noqa: F401
    from app.modules.sync_control.models import SyncExecutionScope
    from app.modules.sync_control.schedule import record_report_outcome

    now = datetime.now(UTC)
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="history",
        advertiser_ids=["acc-1"],
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        due_at=now,
    )
    assert dispatch_due(session, redis_client, now=now, physical_slots=1) == 1
    job = session.get(ReportSyncJob, job_id)
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job_id)
    ).one()
    assert child.execution_generation == job.claim_generation
    assert child.observed_at is None
    job.status, job.error_code = "FAILED", "authorization_revoked"
    record_report_outcome(session, job)
    assert child.status == "SUPERSEDED"
    head = session.get(SyncSchedule, (child.scope_id, "history"))
    assert not head.active and head.wait_reason.startswith("TERMINAL:")


def test_waiting_page_does_not_delay_other_due_bucket_on_same_head(
    session, sync_seed, redis_client
):
    from app.modules.reporting import job_tasks  # noqa: F401
    from app.modules.sync_control.models import SyncExecutionScope
    from app.modules.sync_control.schedule import record_report_outcome

    now = datetime.now(UTC)
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "due_at": now,
    }
    first = enqueue_job(
        session, start=date(2026, 10, 1), end=date(2026, 10, 1), **common
    )
    assert dispatch_due(session, redis_client, now=now, physical_slots=1) == 1
    enqueue_job(session, start=date(2026, 10, 2), end=date(2026, 10, 2), **common)
    job = session.get(ReportSyncJob, first)
    job.status = "WAITING"
    job.due_at = now + timedelta(hours=1)
    record_report_outcome(session, job)
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == first)
    ).one()
    head = session.get(SyncSchedule, (child.scope_id, "history"))
    assert head.active and head.next_eligible_at <= now


def test_ten_thousand_due_scopes_at_daily_quota_do_not_flood_broker(
    session, sync_seed, redis_client, monkeypatch
):
    import hashlib

    from sqlalchemy import event, func, insert

    from app.jobs.admission import admission_keys, admission_policy, admit_call
    from app.modules.reporting import job_tasks  # noqa: F401
    from app.modules.sync_control.models import (
        SyncExecutionScope,
        SyncRequirement,
        SyncScope,
    )

    now = datetime.now(UTC)
    endpoint = "GET /open_api/v1.3/report/integrated/get/"
    policy = quota_configuration("BASIC")
    policy["endpoints"]["OFFICIAL_API:" + endpoint] = {"endpoint_calls_per_day": 1}
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", policy)
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="history",
        advertiser_ids=["acc-1"],
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        due_at=now,
    )
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job_id)
    ).one()
    templates = [
        session.get(SyncScope, child.scope_id).model_dump(),
        session.get(SyncRequirement, child.requirement_id).model_dump(),
        session.get(ReportSyncJob, job_id).model_dump(),
        child.model_dump(),
        session.get(SyncSchedule, (child.scope_id, "history")).model_dump(),
    ]
    # 真实独立查询scope/执行，批量种子仅节省测试准备，不替代正式dispatcher。
    for offset in range(1, 10000, 500):
        batches = [[] for _ in templates]
        for index in range(offset, min(offset + 500, 10000)):
            scope_id, requirement_id, execution_id = uuid4(), uuid4(), uuid4()
            rows = [dict(row) for row in templates]
            rows[0].update(
                id=scope_id,
                identity_digest=hashlib.sha256(
                    f"quota-scope-{index}".encode()
                ).hexdigest(),
                query_contract={
                    "query": {"campaign_ids": [str(index)]},
                    "metrics": "report-v2",
                },
            )
            rows[1].update(id=requirement_id, scope_id=scope_id)
            rows[2].update(
                id=execution_id,
                job_key=f"quota-test-{execution_id}",
                campaign_ids=[str(index)],
            )
            rows[3].update(
                id=uuid4(),
                scope_id=scope_id,
                requirement_id=requirement_id,
                report_job_id=execution_id,
            )
            rows[4].update(scope_id=scope_id)
            for batch, row in zip(batches, rows, strict=True):
                batch.append(row)
        for model, batch in zip(
            (
                SyncScope,
                SyncRequirement,
                ReportSyncJob,
                SyncExecutionScope,
                SyncSchedule,
            ),
            batches,
            strict=True,
        ):
            session.connection().execute(insert(model), batch)
    assert (
        session.exec(
            select(func.count())
            .select_from(SyncSchedule)
            .where(SyncSchedule.tenant_id == sync_seed.tenant_id)
        ).one()
        == 10000
    )
    scope = {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": endpoint,
        "rate_group": endpoint,
        "tenant_id": sync_seed.tenant_id,
        "advertiser_id": "acc-1",
    }
    actual = admission_policy(
        "reports.integrated", channel="OFFICIAL_API", physical_endpoint=endpoint
    )
    assert admit_call(redis_client, lease_id=uuid4(), policy=actual, **scope).granted
    keys = admission_keys(**scope)
    before = [redis_client.zcard(key) for key in keys]
    query_count = [0]

    def count(*_args):
        query_count[0] += 1

    from sqlalchemy import text

    connection = session.connection()
    connection.execute(text("SET LOCAL statement_timeout='5s'"))
    event.listen(connection, "before_cursor_execute", count)
    try:
        for _ in range(3):
            assert dispatch_due(session, redis_client, now=now, physical_slots=16) == 0
    finally:
        event.remove(connection, "before_cursor_execute", count)
    assert (
        session.exec(
            select(func.count())
            .select_from(PendingDispatch)
            .where(PendingDispatch.tenant_id == sync_seed.tenant_id)
        ).one()
        == 0
    )
    assert [redis_client.zcard(key) for key in keys] == before
    assert query_count[0] < 150
    assert not any(
        name.startswith("local_denied")
        for name in redis_client.hgetall(keys[1] + ":metrics")
    )


def test_realtime_requirement_does_not_promote_an_old_multiday_execution(
    session, sync_seed, redis_client
):
    from zoneinfo import ZoneInfo

    from app.modules.reporting import job_tasks  # noqa: F401

    now = datetime.now(UTC)
    yesterday = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date() - timedelta(days=1)
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "advertiser_ids": ["acc-1"],
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "due_at": now,
    }
    old = enqueue_job(
        session,
        **common,
        tier="history",
        start=yesterday - timedelta(days=5),
        end=yesterday,
    )
    urgent = enqueue_job(session, **common, tier="hot", start=yesterday, end=yesterday)
    assert urgent is not None and urgent != old
    assert dispatch_due(session, redis_client, now=now, physical_slots=1) == 1
    assert session.get(ReportSyncJob, urgent).status == "RUNNING"
    assert session.get(ReportSyncJob, old).status == "QUEUED"
