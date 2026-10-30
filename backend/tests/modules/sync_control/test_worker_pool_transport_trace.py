"""四并发消费者读取真实outbox、运行真实Worker/SDK；只替换broker与HTTP运输。"""

import json
import os
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, delete, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import quota_configuration
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.ads import tasks as directory_tasks
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncState
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.models import SyncCostLedger, SyncWorkerPool
from app.modules.sync_control.worker_pool import background_lease_count
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.sync_control.test_cost_transport_trace import clear_trace_case

pytestmark = pytest.mark.skipif(
    os.getenv("SYNC_POOL_TRACE") != "1",
    reason="explicit four-consumer transport benchmark",
)


def clear_pool_case(engine, tenant):
    with Session(engine) as session, session.begin():
        session.exec(delete(AdDirectoryPage).where(AdDirectoryPage.tenant_id == tenant))
    clear_trace_case(engine, tenant)
    with Session(engine) as session, session.begin():
        session.exec(delete(AdDirectoryRun).where(AdDirectoryRun.tenant_id == tenant))
        session.exec(delete(ReportSyncState).where(ReportSyncState.tenant_id == tenant))


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_four_consumers_preserve_success_rate_with_bounded_prefetch(
    database_engine,
    redis_client,
    gateway_case,
    monkeypatch,
):
    from app.jobs import tasks as outbox_tasks

    context, route, advertiser = gateway_case
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    # redis_client fixture已验证TEST与APP不同；仅本例让真实refill共用这个隔离账本。
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    # broker运输替身：下文四个消费者直接领取已持久outbox，保留原payload和任务入口。
    monkeypatch.setattr(outbox_tasks, "drain_dispatch", lambda: 0)
    count = int(os.getenv("SYNC_POOL_TRACE_PER_CLASS", "24"))
    latency = float(os.getenv("SYNC_POOL_TRACE_LATENCY", "0.5"))
    trace, samples, active, high = [], [], 0, 0
    lock = Lock()
    results = {}
    advertisers = [str(int(advertiser) + index + 1) for index in range(count)]
    with Session(database_engine) as session, session.begin():
        original_grant = (
            session.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == context.tenant_id,
                    BCAccountAccess.advertiser_id == advertiser,
                )
            )
            .one()
            .model_dump()
        )
        for account in advertisers:
            session.add(
                AdvertiserAccount(
                    tenant_id=context.tenant_id,
                    advertiser_id=account,
                    currency="USD",
                    timezone="UTC",
                    remote_status="STATUS_ENABLE",
                )
            )
        session.flush()
        for account in advertisers:
            session.add(
                BCAccountAccess(**(original_grant | {"advertiser_id": account}))
            )

    def transport(pool, _method, url, **kwargs):
        nonlocal active, high
        fields = dict(kwargs["fields"])
        cost = getattr(pool, "_sync_attempt_observer", None)
        if cost:
            cost[0].attempt(cost[1])
        begin = time.perf_counter()
        kind = "facts" if "/report/integrated/" in url else "directory"
        with lock:
            active += 1
            high = max(high, active)
        time.sleep(latency)
        with lock:
            active -= 1
            trace.append({"class": kind, "start": begin, "end": time.perf_counter()})
        body = {
            "code": 0,
            "data": {
                "list": [],
                "page_info": {
                    "page": int(fields.get("page", 1)),
                    "page_size": int(fields["page_size"]),
                    "total_page": 0,
                    "total_number": 0,
                },
            },
        }
        return HTTPResponse(
            body=json.dumps(body).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)

    def consume(message):
        name, payload = message
        if name == "reporting.backfill_step":
            result = job_tasks.run_job_step(
                database_engine=database_engine,
                redis_client=redis_client,
                job_id=UUID(payload["job_id"]),
                claim_token=UUID(payload["claim_id"]),
            )
            if result == "FAILED":
                with Session(database_engine) as diagnostic:
                    row = diagnostic.get(ReportSyncJob, UUID(payload["job_id"]))
                    raise AssertionError((name, row.error_code, row.progress))
            return result
        result = directory_tasks.run_directory_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            run_id=UUID(payload["run_id"]),
            claim_generation=payload["claim_generation"],
            publish_only=name == "ads.publish_step",
        )

        if result == "FAILED":
            with Session(database_engine) as diagnostic:
                row = diagnostic.get(AdDirectoryRun, UUID(payload["run_id"]))
                if (
                    row.claim_generation > payload["claim_generation"]
                    and row.status != "FAILED"
                ):
                    return "STALE"
                raise AssertionError((name, row.error_code, row.query))
        return result

    def take_messages(limit, *, publish=False):
        names = (
            ["ads.publish_step"]
            if publish
            else ["reporting.backfill_step", "ads.sync_step"]
        )
        with Session(database_engine) as session, session.begin():
            rows = session.exec(
                select(PendingDispatch)
                .where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    PendingDispatch.task_name.in_(names),
                    PendingDispatch.published_at.is_(None),
                    PendingDispatch.superseded_at.is_(None),
                    PendingDispatch.available_at <= datetime.now(UTC),
                )
                .order_by(PendingDispatch.available_at, PendingDispatch.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            output = [(row.task_name, dict(row.payload)) for row in rows]
            for row in rows:
                row.published_at = datetime.now(UTC)
            return output

    try:
        candidates = [
            int(value)
            for value in os.getenv("SYNC_POOL_TRACE_PREFETCHES", "0,4").split(",")
        ]
        scenarios = [("wide_queue_baseline", 72)] + [
            (f"pool4_prefetch{value}", value) for value in candidates
        ]
        if os.getenv("SYNC_POOL_TRACE_CANDIDATE_FIRST") == "1":
            scenarios.reverse()
        for label, prefetch in scenarios:
            clear_pool_case(database_engine, context.tenant_id)
            trace, samples, active, high = [], [], 0, 0
            with Session(database_engine) as session, session.begin():
                pool = session.get(SyncWorkerPool, "background")
                if pool is None:
                    pool = SyncWorkerPool(
                        pool_name="background",
                        capacity=4,
                        prefetch_slots=prefetch,
                        fairness_state={},
                    )
                    session.add(pool)
                else:
                    pool.capacity, pool.prefetch_slots, pool.fairness_state = (
                        4,
                        prefetch,
                        {},
                    )
                pool.readiness_cursor = {}
                for i, target_advertiser in enumerate(advertisers):
                    day = datetime.now(UTC).date() - timedelta(days=i + 5)
                    enqueue_job(
                        session,
                        tenant_id=context.tenant_id,
                        bc_id=route.bc_id,
                        actor_id=context.actor_id,
                        frozen_route=route.model_dump(mode="json"),
                        advertiser_ids=[target_advertiser],
                        kind="ad_day",
                        tier="history",
                        campaign_ids=[str(i)],
                        start=day,
                        end=day,
                        due_at=datetime.now(UTC),
                    )
                    run = AdDirectoryRun(
                        tenant_id=context.tenant_id,
                        actor_id=context.actor_id,
                        advertiser_id=target_advertiser,
                        bc_id=route.bc_id,
                        connection_id=route.connection_id,
                        channel=route.channel,
                        frozen_route=route.model_dump(mode="json"),
                        partition_key=uuid4().hex * 2,
                        kind="campaign",
                        ad_type="REGULAR",
                        workload="background",
                        claim_generation=1,
                        query={
                            "advertiser_id": target_advertiser,
                            "kind": "campaign",
                            "ad_type": "REGULAR",
                            "page": 1,
                            "page_size": 100 + i,
                            "ids": [],
                            "parent_ids": [],
                            "include_deleted": False,
                        },
                    )
                    session.add(run)
                    session.flush()
                    register_directory_run(session, run, new_execution=True)
            started, tick = time.perf_counter(), 0
            finished = []
            with (
                ThreadPoolExecutor(max_workers=4) as workers,
                ThreadPoolExecutor(max_workers=1) as publisher,
            ):
                inflight, publishes = set(), set()
                while True:
                    elapsed = time.perf_counter() - started
                    assert elapsed < 180, (label, len(finished), len(trace))
                    if elapsed >= tick:
                        job_tasks._dispatch_pending(
                            database_engine, datetime.now(UTC), budget_seconds=2
                        )
                        tick = elapsed + 1
                    for message in take_messages(4 - len(inflight)):
                        inflight.add(workers.submit(consume, message))
                    for message in take_messages(1 - len(publishes), publish=True):
                        publishes.add(publisher.submit(consume, message))
                    done, _ = wait(
                        inflight | publishes, timeout=0.01, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        outcome = future.result()
                        assert outcome in {
                            "COMPLETE",
                            "CONTINUE",
                            "READY",
                            "WAITING",
                            "WAIT",
                            "STALE",
                        }, outcome
                        finished.append(outcome)
                        inflight.discard(future)
                        publishes.discard(future)
                    with Session(database_engine) as session:
                        running = background_lease_count(session, now=datetime.now(UTC))
                        remaining = (
                            session.exec(
                                select(ReportSyncJob.id).where(
                                    ReportSyncJob.tenant_id == context.tenant_id,
                                    ReportSyncJob.status != "COMPLETE",
                                )
                            ).first()
                            or session.exec(
                                select(AdDirectoryRun.id).where(
                                    AdDirectoryRun.tenant_id == context.tenant_id,
                                    AdDirectoryRun.status != "COMPLETE",
                                )
                            ).first()
                        )
                    samples.append(
                        {
                            "time": elapsed,
                            "leased_or_queued": running,
                            "consumers_busy": len(inflight),
                        }
                    )
                    assert running <= 4 + prefetch
                    if remaining is None and not inflight and not publishes:
                        break
            duration = time.perf_counter() - started
            assert Counter(row["class"] for row in trace) == {
                "facts": count,
                "directory": count,
            }
            assert high == 4
            assert "STALE" not in finished
            with Session(database_engine) as audit:
                ledgers = audit.exec(
                    select(SyncCostLedger).where(
                        SyncCostLedger.tenant_id == context.tenant_id
                    )
                ).all()
                assert sum(row.observed_units for row in ledgers) == len(trace)
                assert all(row.closed for row in ledgers)
            results[label] = {
                "http_success": len(trace),
                "seconds": duration,
                "success_per_second": len(trace) / duration,
                "max_http_concurrency": high,
                "outcomes": dict(Counter(finished)),
                "trace": trace,
                "samples": samples,
            }
        baseline = results["wide_queue_baseline"]["success_per_second"]
        # 小样本只验证并发/完整性，不能把启动噪声当成吞吐验收。
        if count >= 24:
            assert (
                max(
                    results[f"pool4_prefetch{value}"]["success_per_second"]
                    for value in candidates
                )
                >= baseline * 0.9
            )
    finally:
        destination = (
            Path(__file__).resolve().parents[4]
            / ".runtime"
            / "worker-pool-transport-trace.json"
        )
        destination.write_text(
            json.dumps(
                {
                    "latency_seconds": latency,
                    "workers": 4,
                    "per_class": count,
                    "variants": results,
                },
                indent=2,
            )
        )
        clear_pool_case(database_engine, context.tenant_id)
        with Session(database_engine) as session, session.begin():
            pool = session.get(SyncWorkerPool, "background")
            if pool:
                pool.capacity, pool.prefetch_slots, pool.fairness_state = 4, 0, {}
