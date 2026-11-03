"""末页与并发扫描只产生一次canonical publisher领取，SDK仅发送真实的一页。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.jobs import outbox
from app.jobs.models import PendingDispatch
from app.modules.ads import tasks
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import (
    SyncCostLedger,
    SyncExecutionScope,
    SyncSchedule,
)
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.integrations.tiktok.test_ads_read_adapters import page, replies
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("targeted", [False, True])
def test_final_page_and_concurrent_scanner_have_one_publisher(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch, targeted
):
    context, route, advertiser = gateway_case
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    # 正式refill读取同一个测试Redis账本；只替换broker运输，不替换调度或observer。
    import os

    monkeypatch.setattr(tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    sent = []
    monkeypatch.setattr(
        outbox.celery_app,
        "send_task",
        lambda name, **kwargs: sent.append((name, kwargs)),
    )
    replies(gateway_wire, "OFFICIAL_API", {"campaign_get": [page([], size=100)]})
    barrier = Barrier(2)
    first_delivery = True
    original = tasks._deliver_committed_work

    def concurrent_delivery(db_engine, work):
        nonlocal first_delivery
        if first_delivery:
            first_delivery = False
            barrier.wait(timeout=10)
        return original(db_engine, work)

    monkeypatch.setattr(tasks, "_deliver_committed_work", concurrent_delivery)
    try:
        with Session(database_engine) as db, db.begin():
            run = AdDirectoryRun(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                advertiser_id=advertiser,
                bc_id=route.bc_id,
                connection_id=route.connection_id,
                channel=route.channel,
                frozen_route=route.model_dump(mode="json"),
                partition_key=uuid4().hex * 2,
                kind="campaign",
                ad_type="REGULAR",
                claim_generation=1,
                query={
                    "advertiser_id": advertiser,
                    "kind": "campaign",
                    "ad_type": "REGULAR",
                    "page": 1,
                    "page_size": 100,
                    "ids": ["one"] if targeted else [],
                    "parent_ids": [],
                    "include_deleted": False,
                },
            )
            db.add(run)
            db.flush()
            identity = run.id
            register_directory_run(db, run, new_execution=True)
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=1,
                )
                == 1
            )
            reading_generation = run.claim_generation
        outbox.flush_dispatch()
        read_message = next(
            kwargs["kwargs"]
            for _, kwargs in sent
            if kwargs["kwargs"]["payload"].get("run_id") == str(identity)
        )
        read_task = tasks.targeted_step if targeted else tasks.sync_step
        sent.clear()

        def scan():
            barrier.wait(timeout=10)
            with Session(database_engine) as db, db.begin():
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=40,
                )
            outbox.flush_dispatch()

        with ThreadPoolExecutor(1) as pool:
            scanning = pool.submit(scan)
            assert read_task.run(**read_message) == "CONTINUE"
            scanning.result(timeout=10)
        with Session(database_engine) as db:
            rows = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    PendingDispatch.task_key.endswith(":page:2"),
                )
            ).all()
            assert len(rows) == 1
            publisher = rows[0]
            assert (
                publisher.task_name == "ads.publish_step"
                and publisher.published_at is not None
            )
            generation = publisher.payload["claim_generation"]
            assert generation in {reading_generation, reading_generation + 1}
            heads = db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == identity)
            ).all()
            assert len(heads) == 1 and heads[0].lease_execution_generation == generation
            ledger = db.exec(
                select(SyncCostLedger).where(
                    SyncCostLedger.directory_run_id == identity,
                    SyncCostLedger.generation == generation,
                )
            ).one()
            # 无竞争时原代交接；head正被扫描器短锁时允许规范换代恢复。
            assert ledger.reserved_units == (
                1 if generation == reading_generation else 0
            )
        assert [name for name, _ in sent] == ["ads.publish_step"]
        assert read_task.run(**read_message) == (
            "WAIT" if generation == reading_generation else "IGNORED"
        )
        assert tasks.publish_step.run(**sent[0][1]["kwargs"]) == "READY"
        with Session(database_engine) as db:
            assert db.get(AdDirectoryRun, identity).status == "COMPLETE"
        assert len(gateway_wire["sdk_calls"]) == 1
    finally:
        with Session(database_engine) as db, db.begin():
            for model in (
                SyncCostLedger,
                SyncExecutionScope,
                AdDirectoryPage,
                AdDirectoryRun,
                ReportDispatchCursor,
            ):
                db.execute(delete(model).where(model.tenant_id == context.tenant_id))
