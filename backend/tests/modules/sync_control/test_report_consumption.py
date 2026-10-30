"""真实SDK运输暂停时重投同一queued claim不能形成第二个HTTP消费者。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Lock

import pytest
from sqlmodel import Session, delete, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import quota_configuration
from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncState
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.models import SyncCostLedger
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.sync_control.test_cost_transport_trace import clear_trace_case


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_duplicate_queued_report_cannot_send_while_first_consumer_is_in_http(
    database_engine,
    redis_client,
    gateway_case,
    monkeypatch,
):
    from contextlib import nullcontext

    context, route, advertiser = gateway_case
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(job_tasks, "_report_refill_after", lambda *_: nullcontext())
    started, release, mutex = Event(), Event(), Lock()
    sends = []

    def transport(pool, _method, _url, **_kwargs):
        cost = getattr(pool, "_sync_attempt_observer", None)
        if cost:
            cost[0].attempt(cost[1])
        with mutex:
            sends.append("send")
            first = len(sends) == 1
        if first:
            started.set()
            assert release.wait(10), "test first consumer was not released"
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "list": [],
                        "page_info": {
                            "page": 1,
                            "page_size": 1000,
                            "total_page": 0,
                            "total_number": 0,
                        },
                    },
                }
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    try:
        now = datetime.now(UTC)
        with Session(database_engine) as session, session.begin():
            identity = enqueue_job(
                session,
                tenant_id=context.tenant_id,
                bc_id=route.bc_id,
                actor_id=context.actor_id,
                frozen_route=route.model_dump(mode="json"),
                kind="ad_day",
                tier="history",
                advertiser_ids=[advertiser],
                start=now.date() - timedelta(days=5),
                end=now.date() - timedelta(days=5),
                due_at=now,
            )
            assert dispatch_due(session, redis_client, now=now, physical_slots=1) == 1
            queued = session.get(ReportSyncJob, identity).claim_token

        def execute():
            return job_tasks.run_job_step(
                database_engine=database_engine,
                redis_client=redis_client,
                job_id=identity,
                claim_token=queued,
            )

        with ThreadPoolExecutor(max_workers=1) as worker:
            first = worker.submit(execute)
            try:
                assert started.wait(5)
                duplicate = execute()
                assert duplicate == "FAILED"
                assert len(sends) == 1
                with Session(database_engine) as session:
                    running = session.get(ReportSyncJob, identity)
                    assert running.status == "RUNNING" and running.claim_token != queued
            finally:
                release.set()
            assert first.result(timeout=10) == "COMPLETE"
        with Session(database_engine) as session:
            assert session.get(ReportSyncJob, identity).status == "COMPLETE"
            costs = session.exec(
                select(SyncCostLedger).where(SyncCostLedger.report_job_id == identity)
            ).all()
            assert sum(row.observed_units for row in costs) == 1
            assert all(row.closed for row in costs)
    finally:
        release.set()
        with Session(database_engine) as session, session.begin():
            session.exec(
                delete(ReportSyncState).where(
                    ReportSyncState.tenant_id == context.tenant_id
                )
            )
        clear_trace_case(database_engine, context.tenant_id)


def test_reader_open_admission_deferral_releases_matching_head_and_queued_keys(
    committed_sync_seed,
    redis_client,
    monkeypatch,
):
    from contextlib import contextmanager, nullcontext

    from app.core.db import engine
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.sync_control.models import SyncSchedule
    from app.modules.sync_control.occupancy import occupancy_keys
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "_report_refill_after", lambda *_: nullcontext())

    @contextmanager
    def deferred(**_kwargs):
        raise AccountAdmissionDeferred(1000)
        yield None

    monkeypatch.setattr(job_tasks, "_reader_for", deferred)
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        identity = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            kind="ad_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=now.date() - timedelta(days=5),
            end=now.date() - timedelta(days=5),
            due_at=now,
        )
        assert dispatch_due(session, redis_client, now=now, physical_slots=1) == 1
        queued = session.get(ReportSyncJob, identity).claim_token
    endpoint = "GET /open_api/v1.3/report/integrated/get/"
    keys = occupancy_keys(
        settings.TIKTOK_APP_ID, endpoint, seed.tenant_id, "acc-1", endpoint
    )
    assert all(redis_client.zscore(key, str(queued)) is not None for key in keys)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=identity,
            claim_token=queued,
        )
        == "WAITING"
    )
    with factory() as session:
        job = session.get(ReportSyncJob, identity)
        head = session.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        assert job.claim_token is None and head.active
        assert head.lease_token is None and head.lease_execution_id is None
        assert head.next_eligible_at == job.due_at
    assert all(redis_client.zscore(key, str(queued)) is None for key in keys)
