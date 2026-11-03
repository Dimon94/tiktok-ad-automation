"""真实 SDK/PG/Redis：读取页公平重领，末页沿同代合法领取直接交接本地发布。"""

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


@pytest.mark.parametrize("expire_grant", [False, True])
@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_two_pages_reclaim_fairness_and_keep_complete_publication(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch, expire_grant
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
    original_transport = gateway_wire["before"]["callback"]

    def response_boundary():
        if expire_grant and len(gateway_wire["sdk_calls"]) == 1:
            # HTTP已合法开始，返回末页时原head过期；真实第二PG连接改变到期时间。
            with Session(database_engine) as db, db.begin():
                head = db.exec(
                    select(SyncSchedule).where(
                        SyncSchedule.lease_execution_id == run_id
                    )
                ).one()
                head.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        original_transport()

    gateway_wire["before"]["callback"] = response_boundary
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
        assert committed[-1].refill and committed[-1].deliver is not expire_grant
        assert (
            tasks.run_directory_step(**arguments, claim_generation=next_generation)
            == "WAIT"
        )
        from app.jobs.models import PendingDispatch
        from app.jobs.tasks import guard_sync_dispatch
        from app.modules.sync_control.dispatch_envelope import dispatch_envelope

        with Session(database_engine) as db:
            run = db.get(AdDirectoryRun, run_id)
            publish_generation = run.claim_generation
            assert publish_generation == next_generation
        if expire_grant:
            # 末页必须仍已提交；只有恢复器重新取得规范grant后才签发发布消息。
            with Session(database_engine) as db, db.begin():
                assert db.exec(
                    select(AdDirectoryPage).where(
                        AdDirectoryPage.run_id == run_id,
                        AdDirectoryPage.complete.is_(True),
                    )
                ).one()
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
        with Session(database_engine) as db:
            publication_message = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    PendingDispatch.task_name == "ads.publish_step",
                )
            ).one()
            assert guard_sync_dispatch(
                database_engine=database_engine,
                task_name=publication_message.task_name,
                tenant_id=str(context.tenant_id),
                actor_id=str(context.actor_id),
                payload=publication_message.payload,
                dispatch=dispatch_envelope(publication_message),
            )
            assert (
                db.exec(
                    select(SyncSchedule).where(
                        SyncSchedule.lease_execution_id == run_id
                    )
                )
                .one()
                .lease_execution_generation
                == publish_generation
            )
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
            assert sum(row.observed_units for row in costs) == 2
            assert all(row.closed for row in costs)
            publication = next(
                row for row in costs if row.generation == publish_generation
            )
            if expire_grant:
                assert publication.reserved_units == publication.observed_units == 0
            else:
                assert publication.reserved_units == publication.observed_units == 1
                assert len(publication.producers) >= 2
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


def test_publication_handoff_cannot_reuse_a_transferred_head(
    committed_sync_seed,
):
    from app.jobs.models import PendingDispatch
    from app.modules.sync_control.directory_demand import handoff_directory_publication
    from tests.modules.sync_control.test_schedule_fencing import _directory

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        first, newer = _directory(db, seed), _directory(db, seed)
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        token = uuid4()
        due = datetime.now(UTC) + timedelta(minutes=4)
        head.lease_execution_id = newer.id
        head.lease_execution_generation = newer.claim_generation
        head.lease_token, head.leased_until = token, due
        db.flush()
        assert not handoff_directory_publication(db, first, now=datetime.now(UTC))
        assert (
            head.lease_execution_id == newer.id
            and head.lease_token == token
            and head.leased_until == due
        )
        assert not db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "ads.publish_step",
            )
        ).all()


@pytest.mark.parametrize(
    "total,want,restricted,legacy_creative",
    [
        (7, 5, False, False),
        (2, 2, False, False),
        (7, 2, True, False),
        (2, 2, False, True),
    ],
)
@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_background_quantum_uses_real_sdk_admission_for_every_page(
    database_engine,
    redis_client,
    gateway_case,
    gateway_wire,
    monkeypatch,
    total,
    want,
    restricted,
    legacy_creative,
):
    from app.integrations.tiktok.platform_quotas import sdk_endpoint
    from app.jobs.admission import admission_keys
    from app.modules.ads.directory_dispatch import directory_step_key
    from app.modules.sync_control import directory_demand, directory_dispatch
    from app.modules.sync_control.occupancy import occupancy_keys

    context, route, advertiser = gateway_case
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    if restricted:
        policies = tasks.settings.TIKTOK_CALL_POLICIES
        monkeypatch.setattr(
            tasks.settings,
            "TIKTOK_CALL_POLICIES",
            {
                **policies,
                "base": {
                    **policies["base"],
                    "endpoint_calls_per_window": 2,
                    "window_ms": 60000,
                },
            },
        )
    committed = []
    monkeypatch.setattr(
        tasks, "_deliver_committed_work", lambda _engine, work: committed.append(work)
    )
    replies(
        gateway_wire,
        "OFFICIAL_API",
        {
            ("ad_get" if legacy_creative else "campaign_get"): [
                page(
                    [
                        {
                            "advertiser_id": advertiser,
                            **(
                                {
                                    "ad_id": f"quantum-{i}",
                                    "smart_plus_ad_id": f"asset-{i}",
                                    "ad_name": f"page-{i}",
                                }
                                if legacy_creative
                                else {
                                    "campaign_id": f"quantum-{i}",
                                    "campaign_name": f"page-{i}",
                                }
                            ),
                            "operation_status": "ENABLE",
                        }
                    ],
                    number=i,
                    size=1,
                    total=total,
                )
                for i in range(1, total + 1)
            ]
        },
    )
    identity = None
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
                kind="creative" if legacy_creative else "campaign",
                ad_type="SMART_PLUS" if legacy_creative else "REGULAR",
                claim_generation=1,
                workload="background",
                query={
                    "advertiser_id": advertiser,
                    "kind": "creative" if legacy_creative else "campaign",
                    "ad_type": "SMART_PLUS" if legacy_creative else "REGULAR",
                    "page": 1,
                    "page_size": 1,
                    "ids": [],
                    "parent_ids": [],
                    "include_deleted": False,
                },
            )
            db.add(run)
            db.flush()
            identity = run.id
            with monkeypatch.context() as old_code:
                if legacy_creative:
                    # 旧head和queued保持原端点；新worker在实际发送边界交接到regular。
                    def old_endpoint(_run):
                        return sdk_endpoint("build.get_ads")

                    old_code.setattr(
                        directory_demand, "directory_endpoint", old_endpoint
                    )
                    old_code.setattr(
                        directory_dispatch, "directory_endpoint", old_endpoint
                    )
                register_directory_run(db, run, new_execution=True)
                assert (
                    dispatch_directories_due(
                        db, redis_client, now=datetime.now(UTC), limit=1
                    )
                    == 1
                )
                endpoint = directory_demand.directory_endpoint(run)
            generation = run.claim_generation
            reservation = directory_step_key(run)
        scope = (
            tasks.settings.TIKTOK_APP_ID,
            endpoint,
            context.tenant_id,
            advertiser,
            endpoint,
        )
        queued = occupancy_keys(*scope)
        actual_endpoint = sdk_endpoint("ads.get_ads") if legacy_creative else endpoint
        active = admission_keys(
            tasks.settings.TIKTOK_APP_ID,
            actual_endpoint,
            context.tenant_id,
            advertiser,
            actual_endpoint,
        )
        assert all(redis_client.zscore(key, reservation) is not None for key in queued)
        previous = gateway_wire["before"]["callback"]
        leases = []

        def boundary():
            # 保留真实gateway、SDK和Redis准入，仅替换PoolManager的HTTP发送。
            index = len(gateway_wire["sdk_calls"])
            assert all(redis_client.zscore(key, reservation) is None for key in queued)
            assert redis_client.zcard(active[3]) == 1
            assert (
                int(redis_client.hget(active[1] + ":metrics", "granted:background"))
                == index + 1
            )
            with Session(database_engine) as observer:
                current = observer.get(AdDirectoryRun, identity)
                leases.append((current.claim_token, current.claimed_until))
                if index:
                    assert observer.get(AdDirectoryPage, (identity, index)) is not None
            previous()

        gateway_wire["before"]["callback"] = boundary
        args = {
            "database_engine": database_engine,
            "redis_client": redis_client,
            "context": context,
            "run_id": identity,
            "claim_generation": generation,
        }
        assert tasks.run_directory_step(**args) == (
            "WAIT" if restricted else "CONTINUE"
        )
        assert len(gateway_wire["sdk_calls"]) == want
        assert len(set(leases)) == 1
        assert all(redis_client.zcard(key) == 0 for key in queued + active[2:])
        with Session(database_engine) as db:
            current = db.get(AdDirectoryRun, identity)
            assert current.next_page == want + 1 and current.claimed_until is None
            assert (
                len(
                    db.exec(
                        select(AdDirectoryPage).where(
                            AdDirectoryPage.run_id == identity
                        )
                    ).all()
                )
                == want
            )
            ledgers = db.exec(
                select(SyncCostLedger).where(
                    SyncCostLedger.directory_run_id == identity
                )
            ).all()
            primary = next(row for row in ledgers if row.physical_endpoint == endpoint)
            assert primary.reserved_units == 5 and all(row.closed for row in ledgers)
            assert {row.physical_endpoint: row.observed_units for row in ledgers} == (
                {endpoint: 0, actual_endpoint: want}
                if legacy_creative
                else {endpoint: want}
            )
            if legacy_creative:
                assert all(
                    "/ad/get/" in call[1] and "/smart_plus/" not in call[1]
                    for call in gateway_wire["sdk_calls"]
                )
        assert committed[-1].refill and committed[-1].deliver == (total == 2)
        assert tasks.run_directory_step(**args) == ("FAILED" if restricted else "WAIT")
        assert len(gateway_wire["sdk_calls"]) == want
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
