"""两页真实 SDK/PG/Redis：每页重回公平领取，只有 HTTP 传输替身。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.modules.ads import tasks
from app.modules.ads.models import AdObject, CampaignNameProjection
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
def test_two_pages_reclaim_fairness_and_keep_complete_publication(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch
):
    context, route, advertiser = gateway_case
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    committed = []
    # 只截住提交后的全局队列发送；下文显式执行同一个正式dispatcher。
    monkeypatch.setattr(
        tasks, "_deliver_committed_work", lambda _engine, work: committed.append(work)
    )
    replies(
        gateway_wire,
        "OFFICIAL_API",
        {
            "campaign_get": [
                page(
                    [
                        {
                            "advertiser_id": advertiser,
                            "campaign_id": f"campaign-{i}",
                            "campaign_name": f"page-{i}",
                            "operation_status": "ENABLE",
                        }
                    ],
                    number=i,
                    size=1,
                    total=2,
                )
                for i in (1, 2)
            ]
        },
    )
    run_id = None
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
                    "page_size": 1,
                    "ids": [],
                    "parent_ids": [],
                    "include_deleted": False,
                },
            )
            db.add(run)
            db.flush()
            run_id = run.id
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
            generation = run.claim_generation
            owner = run.owner_epoch
        arguments = {
            "database_engine": database_engine,
            "redis_client": redis_client,
            "context": context,
            "run_id": run_id,
        }
        assert (
            tasks.run_directory_step(**arguments, claim_generation=generation)
            == "CONTINUE"
        )
        assert committed[-1].refill and not committed[-1].deliver
        assert (
            tasks.run_directory_step(**arguments, claim_generation=generation) == "WAIT"
        )
        with Session(database_engine) as db, db.begin():
            run = db.get(AdDirectoryRun, run_id)
            assert run.next_page == 2 and run.owner_epoch == owner
            observed_at = run.observed_at
            assert not db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == run_id)
            ).all()
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=1,
                )
                == 1
            )
            next_generation = run.claim_generation
            assert next_generation > generation
            child = db.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.directory_run_id == run_id
                )
            ).one()
            assert (
                child.execution_generation == next_generation
                and child.observed_at == observed_at
            )
        assert (
            tasks.run_directory_step(**arguments, claim_generation=generation)
            == "FAILED"
        )
        assert (
            tasks.run_directory_step(**arguments, claim_generation=next_generation)
            == "CONTINUE"
        )
        assert committed[-1].refill and not committed[-1].deliver
        assert (
            tasks.run_directory_step(**arguments, claim_generation=next_generation)
            == "WAIT"
        )
        with Session(database_engine) as db, db.begin():
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=1,
                )
                == 1
            )
            publish_generation = db.get(AdDirectoryRun, run_id).claim_generation
            assert publish_generation > next_generation
        assert (
            tasks.run_directory_step(
                **arguments, claim_generation=publish_generation, publish_only=True
            )
            == "READY"
        )
        with Session(database_engine) as db:
            run = db.get(AdDirectoryRun, run_id)
            assert (
                run.status == "COMPLETE"
                and run.observed_at == observed_at
                and run.owner_epoch == owner
            )
            pages = db.exec(
                select(AdDirectoryPage).where(AdDirectoryPage.run_id == run_id)
            ).all()
            assert {row.page for row in pages} == {1, 2}
            assert {
                row.remote_id
                for row in db.exec(
                    select(AdObject).where(AdObject.tenant_id == context.tenant_id)
                ).all()
            } == {"campaign-1", "campaign-2"}
            costs = db.exec(
                select(SyncCostLedger).where(SyncCostLedger.directory_run_id == run_id)
            ).all()
            assert {row.generation for row in costs} == {
                generation,
                next_generation,
                publish_generation,
            }
            publication = next(
                row for row in costs if row.generation == publish_generation
            )
            assert publication.reserved_units == publication.observed_units == 0
            assert publication.closed
            assert all(
                row.reserved_units == 1 and row.observed_units == 1 and row.closed
                for row in costs
                if row.generation != publish_generation
            ), [
                (r.physical_endpoint, r.reserved_units, r.observed_units, r.closed)
                for r in costs
            ]
        assert len(gateway_wire["sdk_calls"]) == 2
    finally:
        with Session(database_engine) as db, db.begin():
            for model in (
                SyncCostLedger,
                SyncExecutionScope,
                AdDirectoryPage,
                AdDirectoryRun,
                AdObject,
                CampaignNameProjection,
                ReportDispatchCursor,
            ):
                db.execute(delete(model).where(model.tenant_id == context.tenant_id))


def test_class_arriving_between_pages_is_in_the_next_cost_vector(
    session, sync_seed, redis_client
):
    from app.modules.sync_control.costs import record_cost_receipt
    from app.modules.sync_control.directory_demand import (
        directory_endpoint,
        directory_outcome,
    )
    from app.modules.sync_control.occupancy import release_dispatch
    from tests.modules.sync_control.test_schedule_fencing import _directory

    run = _directory(session, sync_seed)
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    first = session.exec(
        select(SyncCostLedger).where(SyncCostLedger.directory_run_id == run.id)
    ).one()
    assert first.eligible_weights == {"directory": 15}
    # 这里验证调度/记账合同；上一真实SDK用例单独保证物理发送及完整分页。
    record_cost_receipt(session, first.id, uuid4(), total_units=1, closed=True)
    from app.modules.ads.directory_dispatch import directory_step_key

    release_dispatch(
        redis_client,
        reservation_id=directory_step_key(run),
        app_scope=tasks.settings.TIKTOK_APP_ID,
        endpoint=directory_endpoint(run),
        rate_group=directory_endpoint(run),
        tenant_id=run.tenant_id,
        advertiser_id=run.advertiser_id,
    )
    run.next_page = 2
    directory_outcome(session, run)
    urgent = _directory(
        session, sync_seed, query_extra={"directory_service_v1": "status"}
    )
    session.flush()
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    costs = session.exec(
        select(SyncCostLedger).where(SyncCostLedger.directory_run_id == urgent.id)
    ).all()
    assert len(costs) == 1 and costs[0].work_class == "realtime"
    assert costs[0].eligible_weights == {"directory": 15, "realtime": 50}
    assert session.get(AdDirectoryRun, run.id).next_page == 2
