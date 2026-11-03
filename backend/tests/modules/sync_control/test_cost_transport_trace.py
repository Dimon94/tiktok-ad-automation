"""可选真实SDK/PG/Redis运输压测；不访问平台，保留原BASIC准入合同。"""

import json
import os
import time
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, delete, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.integrations.tiktok.platform_quotas import quota_configuration
from app.jobs.models import PendingDispatch
from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportDispatchCursor, ReportSyncJob
from app.modules.sync_control import costs, dispatcher
from app.modules.sync_control.models import (
    SyncCostLedger,
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy

pytestmark = pytest.mark.skipif(
    os.getenv("SYNC_COST_TRACE") != "1", reason="explicit local transport benchmark"
)


def clear_trace_case(engine, tenant):
    with Session(engine) as session, session.begin():
        for model in (
            SyncCostLedger,
            SyncExecutionScope,
            SyncSchedule,
            SyncRequirement,
            SyncScope,
            PendingDispatch,
            ReportSyncJob,
            ReportDispatchCursor,
        ):
            session.exec(delete(model).where(model.tenant_id == tenant))


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_physical_fifty_vs_five_cost_and_success_throughput(
    database_engine,
    redis_client,
    gateway_case,
    monkeypatch,
):
    context, route, advertiser = gateway_case
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    # 调度在下文显式逐步运行；只截提交后的补位，执行器/分页/围栏/SDK均是真路径。
    monkeypatch.setattr(job_tasks, "_report_refill_after", lambda *_: nullcontext())
    target = int(os.getenv("SYNC_COST_TRACE_SENDS", "400"))
    latency = float(os.getenv("SYNC_COST_TRACE_LATENCY", "0.1"))
    today = datetime.now(UTC).date()
    results = {}
    original_reserve = dispatcher.reserve_execution_cost
    original_settle = costs.settle_costs
    try:
        for variant in ("unit_turn_baseline", "actual_send_cost"):
            clear_trace_case(database_engine, context.tenant_id)
            trace = []

            def transport(pool, _method, url, _trace=trace, **kwargs):
                fields = dict(kwargs["fields"])
                number = int(fields["page"])
                day = fields["start_date"]
                cost = getattr(pool, "_sync_attempt_observer", None)
                if cost:
                    cost[0].attempt(cost[1])
                begin = time.perf_counter()
                time.sleep(latency)
                _trace.append(
                    {
                        "class": "realtime" if day == str(today) else "history",
                        "page": number,
                        "endpoint": url.split("?")[0],
                        "start": begin,
                        "end": time.perf_counter(),
                    }
                )
                # 合成1000页快照只用于持续积压；本次不抵达末页，不伪报完整发布。
                body = {
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "dimensions": {
                                    "stat_time_day": day,
                                    "main_material_id": f"m-{number}-{index}",
                                    "main_material_type": "VIDEO",
                                },
                                "metrics": {"advertiser_id": advertiser, "spend": "1"},
                            }
                            for index in range(100)
                        ],
                        "page_info": {
                            "page": number,
                            "page_size": 100,
                            "total_page": 1000,
                            "total_number": 100000,
                        },
                    },
                }
                return HTTPResponse(
                    body=json.dumps(body).encode(),
                    status=200,
                    headers={"Content-Type": "application/json"},
                )

            monkeypatch.setattr("urllib3.PoolManager.request", transport)
            if variant == "unit_turn_baseline":

                def unit_reserve(*args, **kwargs):
                    kwargs["reserved_units"] = 1
                    return original_reserve(*args, **kwargs)

                monkeypatch.setattr(dispatcher, "reserve_execution_cost", unit_reserve)
                monkeypatch.setattr(costs, "settle_costs", lambda *_args, **_kwargs: 0)
            else:
                monkeypatch.setattr(
                    dispatcher, "reserve_execution_cost", original_reserve
                )
                monkeypatch.setattr(costs, "settle_costs", original_settle)
            with Session(database_engine) as session, session.begin():
                for day, tier in (
                    (today, "hot"),
                    (today - timedelta(days=5), "history"),
                ):
                    enqueue_job(
                        session,
                        tenant_id=context.tenant_id,
                        bc_id=route.bc_id,
                        actor_id=context.actor_id,
                        frozen_route=route.model_dump(mode="json"),
                        advertiser_ids=[advertiser],
                        kind="material_day",
                        tier=tier,
                        start=day,
                        end=day,
                        due_at=datetime.now(UTC),
                    )
            started, steps = time.perf_counter(), []
            while len(trace) < target:
                now = datetime.now(UTC)
                with Session(database_engine) as session, session.begin():
                    claimed = dispatcher.dispatch_due(
                        session, redis_client, now=now, physical_slots=1
                    )
                    if claimed:
                        job = session.exec(
                            select(ReportSyncJob).where(
                                ReportSyncJob.tenant_id == context.tenant_id,
                                ReportSyncJob.status == "RUNNING",
                            )
                        ).one()
                        identity, token, kind = (
                            job.id,
                            job.claim_token,
                            "realtime" if job.start_date == today else "history",
                        )
                if not claimed:
                    assert time.perf_counter() - started < 180, (
                        "persistent head failed to resume"
                    )
                    time.sleep(0.01)
                    continue
                before = len(trace)
                state = job_tasks.run_job_step(
                    database_engine=database_engine,
                    redis_client=redis_client,
                    job_id=identity,
                    claim_token=token,
                )
                with Session(database_engine) as diagnostic:
                    current = diagnostic.get(ReportSyncJob, identity)
                    assert state == "WAITING", (
                        state,
                        current.error_code,
                        current.progress,
                        trace[-1:],
                    )
                assert len(trace) > before, "no successful HTTP in a claimed step"
                steps.append({"class": kind, "sends": len(trace) - before})
            elapsed = time.perf_counter() - started
            counts = dict(Counter(row["class"] for row in trace))
            with Session(database_engine) as session:
                observed = sum(
                    row.observed_units
                    for row in session.exec(
                        select(SyncCostLedger).where(
                            SyncCostLedger.tenant_id == context.tenant_id
                        )
                    ).all()
                )
            assert observed == len(trace)
            assert all(
                step["sends"] == (5 if step["class"] == "realtime" else 50)
                for step in steps
            )
            results[variant] = {
                "counts": counts,
                "http_success": len(trace),
                "seconds": elapsed,
                "success_per_second": len(trace) / elapsed,
                "steps": steps,
                "trace": trace,
            }
        baseline, actual = results.values()
        assert actual["counts"]["realtime"] / actual["http_success"] > 0.5
        assert baseline["counts"]["realtime"] / baseline["http_success"] < 0.3
        # 保留原50/5预算及同一运输延迟；调度/成本持久化不能把成功发送率降低10%以上。
        assert actual["success_per_second"] >= baseline["success_per_second"] * 0.9
    finally:
        destination = (
            Path(__file__).resolve().parents[4]
            / ".runtime"
            / "actual-cost-transport-trace.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {"latency_seconds": latency, "target": target, "variants": results},
                indent=2,
            )
        )
        clear_trace_case(database_engine, context.tenant_id)
