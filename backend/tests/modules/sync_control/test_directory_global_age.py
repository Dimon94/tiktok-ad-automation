"""积压跨过64头后仍按实际服务时间轮转，UUID不能决定谁先执行。"""

from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.sync_control.directory_dispatch import (
    _candidate,
    dispatch_directories_due,
)
from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule
from tests.modules.sync_control.test_schedule_fencing import _directory


def _runs(session, seed, count, prefix):
    now = datetime.now(UTC)
    for i in range(count):
        advertiser = f"{prefix}-{i}"
        session.add(
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id=advertiser,
                currency="USD",
                timezone="UTC",
            )
        )
    session.flush()
    for i in range(count):
        session.add(
            BCAccountAccess(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                advertiser_id=f"{prefix}-{i}",
                connection_id=seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=now,
            )
        )
    session.flush()
    return [_directory(session, seed, advertiser=f"{prefix}-{i}") for i in range(count)]


def _head(session, run):
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.directory_run_id == run.id)
    ).one()
    return session.get(SyncSchedule, (child.scope_id, "directory"))


def test_oldest_qualified_head_outside_first_uuid_page_is_discovered(
    session, sync_seed
):
    runs = _runs(session, sync_seed, 65, "age")
    heads = sorted(
        ((_head(session, r), r) for r in runs), key=lambda item: item[0].scope_id
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    for h, _ in heads:
        h.last_dispatched_at = now - timedelta(minutes=1)
    oldest, run = heads[-1]
    oldest.last_dispatched_at = now - timedelta(days=6)
    found = _candidate(
        session,
        tenant_id=sync_seed.tenant_id,
        work_class="directory",
        now=now,
        independent_only=False,
        discover_only=True,
    )
    assert found[1].id == run.id


def test_next_dispatch_does_not_skip_an_older_uuid_prefix(
    session, sync_seed, redis_client
):
    from app.modules.ads import tasks  # noqa: F401

    runs = _runs(session, sync_seed, 3, "prefix")
    heads = sorted(
        ((_head(session, r), r) for r in runs), key=lambda item: item[0].scope_id
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    ages = [60, 120, 1]
    for (h, _), age in zip(heads, ages, strict=True):
        h.last_dispatched_at = now - timedelta(minutes=age)
    assert (
        dispatch_directories_due(session, redis_client, now=now, limit=40, max_claims=1)
        == 1
    )
    assert heads[1][0].lease_execution_id == heads[1][1].id
    assert (
        dispatch_directories_due(session, redis_client, now=now, limit=40, max_claims=1)
        == 1
    )
    assert heads[0][0].lease_execution_id == heads[0][1].id


def test_hot_heads_cannot_keep_age_scan_from_wrapping_to_newly_due_old_head(
    session, sync_seed, redis_client
):
    from app.modules.ads import tasks  # noqa: F401

    runs = _runs(session, sync_seed, 3, "wrap")
    heads = [_head(session, r) for r in runs]
    now = datetime.now(UTC) + timedelta(seconds=1)
    for h, age in zip(heads, [120, 60, 1], strict=True):
        h.last_dispatched_at = now - timedelta(minutes=age)
    heads[0].next_eligible_at = now + timedelta(seconds=1)
    served = []
    for step in range(4):
        at = now + timedelta(seconds=2 * step)
        assert (
            dispatch_directories_due(
                session, redis_client, now=at, limit=40, max_claims=1
            )
            == 1
        )
        winner = next(h for h in heads if h.lease_execution_id is not None)
        served.append(winner.scope_id)
        if winner.scope_id == heads[0].scope_id:
            break
        # 两个热点头持续完成一页再到期。扫描必须有限回绕，不能无限追逐其新服务时间。
        winner.lease_token = winner.leased_until = None
        winner.lease_execution_id = winner.lease_execution_generation = None
        winner.next_eligible_at = at
    assert heads[0].scope_id in served


def test_saved_empty_age_cycle_reopens_when_later_work_arrives(
    session, sync_seed, redis_client
):
    from app.modules.ads import tasks  # noqa: F401

    _runs(session, sync_seed, 1, "empty-first")
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert (
        dispatch_directories_due(session, redis_client, now=now, limit=40, max_claims=1)
        == 1
    )
    args = {
        "tenant_id": sync_seed.tenant_id,
        "work_class": "directory",
        "independent_only": False,
        "discover_only": True,
        "redis": redis_client,
    }
    # 第一头有正常租约：扫到末端后持久保存了一个空轮次。
    assert _candidate(session, now=now + timedelta(seconds=1), **args) is None
    later = _runs(session, sync_seed, 1, "empty-later")[0]
    _head(session, later).last_dispatched_at = now + timedelta(seconds=2)
    found = _candidate(session, now=now + timedelta(seconds=3), **args)
    assert found is not None and found[1].id == later.id
