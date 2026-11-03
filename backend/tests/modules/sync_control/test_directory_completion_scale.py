"""千头真实PG/Redis/官方SDK传输回归：老快照真正发布，新需求仍获得服务。"""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, delete, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.ads import tasks
from app.modules.ads.models import AdObject, CampaignNameProjection
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncSchedule, SyncWorkerPool
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.ads.test_material_directory_children import (
    durable_material_case as durable_material_case,
)

pytestmark = pytest.mark.skipif(
    os.getenv("SYNC_COMPLETION_SCALE") != "1",
    reason="explicit committed PostgreSQL/Redis/SDK completion benchmark",
)


@pytest.mark.parametrize("initial_heads", [3, 1000])
@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_thousand_heads_old_snapshots_publish_while_new_work_arrives(
    request,
    database_engine,
    redis_client,
    durable_material_case,
    monkeypatch,
    initial_heads,
):
    context, route, advertiser = durable_material_case

    pool_before = None

    # 持久夹具会按真实外键清理账户，先删除本例新增的名称投影。
    def cleanup_projection():
        with Session(database_engine) as db, db.begin():
            pool = db.get(SyncWorkerPool, "background")
            if pool is not None:
                db.delete(pool)
                db.flush()
            if pool_before is not None:
                db.add(SyncWorkerPool(**pool_before))
            db.exec(
                delete(CampaignNameProjection).where(
                    CampaignNameProjection.tenant_id == context.tenant_id
                )
            )

    request.addfinalizer(cleanup_projection)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    # broker边界由下方显式取真实outbox，避免自动补位另行生成无人消费的消息。
    monkeypatch.setattr(tasks, "_deliver_committed_work", lambda *_: None)
    accounts = [advertiser, str(int(advertiser) + 1), str(int(advertiser) + 2)]
    old_ids, arrival_ids, sends, grants = [], set(), [], []
    old_size = 1000 if initial_heads == 1000 else 20
    now = datetime.now(UTC)

    def add_run(db, account, size, age):
        run = AdDirectoryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            advertiser_id=account,
            bc_id=route.bc_id,
            connection_id=route.connection_id,
            channel=route.channel,
            frozen_route=route.model_dump(mode="json"),
            partition_key=uuid4().hex * 2,
            kind="campaign",
            ad_type="REGULAR",
            workload="background",
            claim_generation=1,
            created_at=age,
            query={
                "advertiser_id": account,
                "kind": "campaign",
                "ad_type": "REGULAR",
                "page": 1,
                "page_size": size,
                "ids": [],
                "parent_ids": [],
                "include_deleted": age < now - timedelta(days=1),
            },
        )
        db.add(run)
        db.flush()
        register_directory_run(db, run, new_execution=True)
        return run

    with Session(database_engine) as db, db.begin():
        grant = (
            db.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == context.tenant_id,
                    BCAccountAccess.advertiser_id == advertiser,
                )
            )
            .one()
            .model_dump()
        )
        for account in accounts[1:]:
            db.add(
                AdvertiserAccount(
                    tenant_id=context.tenant_id,
                    advertiser_id=account,
                    currency="USD",
                    timezone="UTC",
                )
            )
        db.flush()
        for account in accounts[1:]:
            db.add(BCAccountAccess(**(grant | {"advertiser_id": account})))
        db.flush()
        pool = db.get(SyncWorkerPool, "background")
        pool_before = pool.model_dump() if pool is not None else None
        if pool is None:
            db.add(
                SyncWorkerPool(pool_name="background", capacity=4, prefetch_slots=12)
            )
        else:
            pool.capacity, pool.prefetch_slots = 4, 12
            pool.fairness_state, pool.readiness_cursor = {}, {}
        for index, account in enumerate(accounts):
            old_ids.append(
                add_run(db, account, old_size, now - timedelta(days=7 - index)).id
            )
        for index in range(initial_heads - 3):
            add_run(db, accounts[index % 3], index + 1, now - timedelta(hours=1))
        # 原普通轮转中，千头待服务前缀在这些大快照之前；完成优先必须越过该等待。
        for head in db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == context.tenant_id)
        ).all():
            head.last_dispatched_at = now - timedelta(
                hours=1 if head.oldest_pending_at < now - timedelta(days=1) else 2
            )

    def transport(pool, _method, url, **kwargs):
        assert "/campaign/get/" in url, url
        fields = dict(kwargs["fields"])
        observer = getattr(pool, "_sync_attempt_observer", None)
        if observer:
            observer[0].attempt(observer[1])
        page, size = int(fields.get("page", 1)), int(fields["page_size"])
        account = str(fields["advertiser_id"])
        sends.append((account, size, page))
        pages = 11 if size == old_size else 1
        body = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "advertiser_id": account,
                        "campaign_id": f"{account}-{size}-{page}-{row}",
                        "campaign_name": "local-completion-scale",
                        "operation_status": "ENABLE",
                    }
                    for row in range(size)
                ],
                "page_info": {
                    "page": page,
                    "page_size": size,
                    "total_page": pages,
                    "total_number": pages * size,
                },
            },
        }
        return HTTPResponse(
            body=json.dumps(body).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    started = time.perf_counter()
    for turn in range(45):
        with Session(database_engine) as db, db.begin():
            arrival_ids.add(
                add_run(db, accounts[turn % 3], 100 + turn, datetime.now(UTC)).id
            )
        dispatch_deadline = time.monotonic() + 10
        while True:
            with Session(database_engine) as db, db.begin():
                claimed = dispatch_directories_due(
                    db, redis_client, now=datetime.now(UTC), limit=40, max_claims=1
                )
                message = db.exec(
                    select(PendingDispatch)
                    .where(
                        PendingDispatch.tenant_id == context.tenant_id,
                        PendingDispatch.task_name.in_(
                            ["ads.sync_step", "ads.publish_step"]
                        ),
                        PendingDispatch.published_at.is_(None),
                        PendingDispatch.superseded_at.is_(None),
                        PendingDispatch.available_at <= datetime.now(UTC),
                    )
                    .order_by(PendingDispatch.available_at, PendingDispatch.id)
                ).first()
                if message is not None:
                    message.published_at = datetime.now(UTC)
                    identity, generation = (
                        UUID(message.payload["run_id"]),
                        message.payload["claim_generation"],
                    )
                    grants.append(identity)
                    break
                if time.monotonic() >= dispatch_deadline:
                    states = [
                        (
                            str(r.id),
                            r.status,
                            r.next_page,
                            str(r.next_attempt_at),
                            str(r.claimed_until),
                        )
                        for r in db.exec(
                            select(AdDirectoryRun).where(AdDirectoryRun.id.in_(old_ids))
                        ).all()
                    ]
                    pool = db.get(SyncWorkerPool, "background")
                    raise AssertionError(
                        {
                            "turn": turn,
                            "claimed": claimed,
                            "old": states,
                            "pool": pool.model_dump() if pool else None,
                        }
                    )
            # 保留真实配额/准入退避，不能要求每一次瞬时轮询必定获派发。
            time.sleep(0.05)
        result = tasks.run_directory_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            run_id=identity,
            claim_generation=generation,
        )
        assert result in {"CONTINUE", "READY"}, (identity, result)
        with Session(database_engine) as db:
            run = db.get(AdDirectoryRun, identity)
            is_terminal = run.next_page == (12 if identity in old_ids else 2)
        if is_terminal:
            assert (
                tasks.run_directory_step(
                    database_engine=database_engine,
                    redis_client=redis_client,
                    context=context,
                    run_id=identity,
                    claim_generation=generation,
                    publish_only=True,
                )
                == "READY"
            )
            with Session(database_engine) as db, db.begin():
                for message in db.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == context.tenant_id,
                        PendingDispatch.task_name == "ads.publish_step",
                        PendingDispatch.published_at.is_(None),
                    )
                ).all():
                    message.published_at = datetime.now(UTC)
        with Session(database_engine) as db:
            old = [db.get(AdDirectoryRun, identity) for identity in old_ids]
            if all(
                r.status == "COMPLETE" and r.published_version is not None for r in old
            ):
                break
    with Session(database_engine) as db:
        old = [db.get(AdDirectoryRun, identity) for identity in old_ids]
        assert all(
            r.status == "COMPLETE" and r.published_version is not None for r in old
        ), [(r.next_page, r.status) for r in old]
        assert (
            db.exec(
                select(AdObject).where(AdObject.tenant_id == context.tenant_id)
            ).first()
            is not None
        )
        completed_new = db.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == context.tenant_id,
                AdDirectoryRun.id.not_in(old_ids),
                AdDirectoryRun.status == "COMPLETE",
            )
        ).all()
        assert len(completed_new) >= (3 if initial_heads == 1000 else 1)
        completed_arrivals = {r.id for r in completed_new} & arrival_ids
        if initial_heads == 3:
            assert completed_arrivals  # 明确证明持续旧工作期间本轮新到需求已服务。
    assert all(
        sum(1 for a, s, _ in sends if a == account and s == old_size) == 11
        for account in accounts
    )
    assert len(set(grants) - set(old_ids)) >= (3 if initial_heads == 1000 else 1)
    print(  # noqa: T201 - 压测证据随pytest -s保存，不含真实业务数据。
        {
            "heads": initial_heads,
            "turns": len(grants),
            "http": len(sends),
            "old_complete": len(old_ids),
            "other_complete": len(completed_new),
            "arrivals_complete": len(completed_arrivals),
            "seconds": round(time.perf_counter() - started, 3),
        }
    )
