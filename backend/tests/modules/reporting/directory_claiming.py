"""目录调度回归使用正式需求登记、成本回执、完成围栏和真实Redis占位。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.directory_dispatch import directory_step_key
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.contracts import ExecutionFence
from app.modules.sync_control.costs import record_cost_receipt
from app.modules.sync_control.directory_demand import (
    directory_endpoint,
    directory_outcome,
    register_directory_run,
)
from app.modules.sync_control.models import SyncCostLedger
from app.modules.sync_control.occupancy import release_dispatch
from app.modules.sync_control.requirements import satisfy_requirements
from tests.modules.reporting.test_directory_direct_delivery import directory


def pending(
    session,
    seed,
    service="status",
    *,
    advertiser="acc-1",
    age=0,
    background=False,
    full=False,
):
    if session.get(AdvertiserAccount, (seed.tenant_id, advertiser)) is None:
        session.add(
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id=advertiser,
                timezone="UTC",
                currency="USD",
            )
        )
        session.flush()
    run = directory(session, seed)
    run.advertiser_id = advertiser
    run.created_at = datetime.now(UTC) - timedelta(minutes=age)
    run.workload = "background" if background else "live"
    run.query = run.query | {
        "advertiser_id": advertiser,
        "directory_service_v1": service,
    }
    if full:
        run.query = run.query | {"ids": [], "include_deleted": True}
    session.flush()
    register_directory_run(session, run, new_execution=True)
    session.flush()
    return run


def sent(session):
    return [
        session.get(AdDirectoryRun, UUID(p.payload["run_id"]))
        for p in session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name.in_(
                    ["ads.sync_step", "ads.targeted_step", "ads.publish_step"]
                )
            )
        ).all()
    ]


def finish(session, redis, run, *, now):
    # 模拟已取得正式派发的执行完成，仅不调用外部传输；不伪造调度租约。
    endpoint = directory_endpoint(run)
    release_dispatch(
        redis,
        reservation_id=directory_step_key(run),
        app_scope=settings.TIKTOK_APP_ID,
        endpoint=endpoint,
        rate_group=endpoint,
        tenant_id=run.tenant_id,
        advertiser_id=run.advertiser_id,
    )
    for ledger in session.exec(
        select(SyncCostLedger).where(
            SyncCostLedger.directory_run_id == run.id,
            SyncCostLedger.generation == run.claim_generation,
        )
    ).all():
        record_cost_receipt(session, ledger.id, uuid4(), total_units=1, closed=True)
    run.status, run.published_version, run.observed_at = "COMPLETE", 1, now
    satisfy_requirements(
        session,
        fence=ExecutionFence(
            run.id, "directory", run.claim_generation, run.owner_epoch
        ),
        observed_at=now,
        publication_version=1,
    )
    directory_outcome(session, run)
    session.flush()
