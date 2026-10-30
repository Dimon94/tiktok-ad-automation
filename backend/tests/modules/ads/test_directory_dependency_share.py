"""背景依赖和全量共用canonical类；有限积压都推进且总执行窗口不可超额。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.accounts.models import AdvertiserAccount
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncWorkerPool
from tests.modules.ads.test_directory_sync import _run
from tests.modules.reporting.directory_claiming import finish, sent
from tests.modules.sync_control.conftest import (
    isolated_sync_quota as isolated_sync_quota,
)


@pytest.fixture(autouse=True)
def eight_slot_background_pool(session):
    # 本组专测目录8槽，显式给共享池同样8槽，避免默认4槽成为另一个瓶颈。
    pool = session.get(SyncWorkerPool, "background")
    if pool is None:
        pool = SyncWorkerPool(pool_name="background", capacity=8)
        session.add(pool)
    pool.capacity, pool.prefetch_slots = 8, 0
    session.flush()


def _pending(session, seed, *, targeted, age, advertiser):
    session.add(
        AdvertiserAccount(
            tenant_id=seed.context.tenant_id,
            advertiser_id=advertiser,
            currency="USD",
            timezone="UTC",
        )
    )
    row = _run(session, seed, kind="ad", ad_type="SMART_PLUS")
    row.advertiser_id = advertiser
    row.workload, row.created_at = "background", age
    row.query = row.query | {
        "advertiser_id": advertiser,
        "ids": [str(row.id)] if targeted else [],
        "include_deleted": True,
    }
    session.flush()
    register_directory_run(session, row, new_execution=True)
    return row


def tick(session, redis):
    return dispatch_directories_due(
        session, redis, now=datetime.now(UTC) + timedelta(seconds=1), limit=8
    )


def test_background_dependency_and_full_scopes_both_progress(
    session, directory_seed, redis_client
):
    now = datetime.now(UTC)
    full = [
        _pending(
            session,
            directory_seed,
            targeted=False,
            age=now - timedelta(hours=5, seconds=i),
            advertiser=f"full-{i}",
        )
        for i in range(16)
    ]
    dependencies = [
        _pending(
            session,
            directory_seed,
            targeted=True,
            age=now - timedelta(minutes=10, seconds=i),
            advertiser=f"dependency-{i}",
        )
        for i in range(8)
    ]
    expected = {r.id for r in full + dependencies}
    completed = set()
    for _ in range(3):
        assert tick(session, redis_client) == 8
        active = [r for r in sent(session) if r.status != "COMPLETE"]
        assert len(active) == 8 and all(r.workload == "background" for r in active)
        assert tick(session, redis_client) == 0
        for row in active:
            finish(session, redis_client, row, now=datetime.now(UTC))
            completed.add(row.id)
    assert completed == expected


@pytest.mark.parametrize("occupied_targeted", [True, False])
def test_background_share_accounts_for_existing_claims(
    session, directory_seed, redis_client, occupied_targeted
):
    now = datetime.now(UTC)
    occupied = [
        _pending(
            session,
            directory_seed,
            targeted=occupied_targeted,
            age=now - timedelta(days=1),
            advertiser=f"occupied-{i}",
        )
        for i in range(7)
    ]
    assert tick(session, redis_client) == 7
    for row in occupied:
        row.status, row.claimed_until = "RUNNING", now + timedelta(minutes=1)
    desired = _pending(
        session,
        directory_seed,
        targeted=not occupied_targeted,
        age=now - timedelta(minutes=1),
        advertiser="remaining",
    )
    assert tick(session, redis_client) == 1
    assert {r.id for r in sent(session)} == {r.id for r in occupied} | {desired.id}
    assert tick(session, redis_client) == 0


@pytest.mark.parametrize("targeted", [True, False])
def test_background_unused_share_is_borrowed(
    session, directory_seed, redis_client, targeted
):
    now = datetime.now(UTC)
    for index in range(9):
        _pending(
            session,
            directory_seed,
            targeted=targeted,
            age=now - timedelta(hours=1),
            advertiser=f"only-{index}",
        )
    assert tick(session, redis_client) == 8
    assert len(sent(session)) == 8
    assert tick(session, redis_client) == 0
