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


def test_temporarily_locked_oldest_head_is_retried_after_other_work_finishes(
    committed_sync_seed, redis_client
):
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        runs = _runs(db, seed, 3, "lock-age")
        ids = [r.id for r in runs]
        now = datetime.now(UTC) + timedelta(seconds=1)
        for r, age in zip(runs, [120, 60, 30], strict=True):
            _head(db, r).last_dispatched_at = now - timedelta(minutes=age)
    with factory() as blocker, blocker.begin():
        blocker.get(AdDirectoryRun, ids[0], with_for_update=True)
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db, redis_client, now=now, limit=40, max_claims=1
                )
                == 1
            )
            assert (
                _head(db, db.get(AdDirectoryRun, ids[1])).lease_execution_id == ids[1]
            )
    with factory() as db, db.begin():
        assert (
            dispatch_directories_due(
                db, redis_client, now=now + timedelta(seconds=1), limit=40, max_claims=1
            )
            == 1
        )
        assert _head(db, db.get(AdDirectoryRun, ids[0])).lease_execution_id == ids[0]


def test_lock_prefix_larger_than_retry_budget_cannot_lose_old_work_forever(
    committed_sync_seed, redis_client
):
    from app.modules.ads import tasks  # noqa: F401
    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        runs = _runs(db, seed, 67, "many-lock-age")
        ids = [r.id for r in runs]
        now = datetime.now(UTC) + timedelta(seconds=1)
        for i, r in enumerate(runs):
            _head(db, r).last_dispatched_at = (
                now - timedelta(days=2) + timedelta(seconds=i)
            )
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id.in_(ids[:65]))
            .with_for_update()
        ).all()
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db, redis_client, now=now, limit=40, max_claims=1
                )
                == 0
            )
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db, redis_client, now=now, limit=40, max_claims=1
                )
                == 1
            )
            assert (
                _head(db, db.get(AdDirectoryRun, ids[65])).lease_execution_id == ids[65]
            )
    with factory() as db, db.begin():
        assert (
            dispatch_directories_due(
                db, redis_client, now=now + timedelta(seconds=1), limit=40, max_claims=1
            )
            == 1
        )
        assert _head(db, db.get(AdDirectoryRun, ids[0])).lease_execution_id == ids[0]


def test_successful_claims_reuse_verified_ineligible_prefix(
    committed_sync_seed, redis_client
):
    from uuid import UUID

    from sqlalchemy import event

    from app.modules.ads import tasks  # noqa: F401
    from app.modules.sync_control.models import SyncScope

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        runs = _runs(db, seed, 2, "cached-empty")
        now = datetime.now(UTC) + timedelta(seconds=1)
        first = _head(db, runs[0])
        scope = db.get(SyncScope, first.scope_id)
        for index, run in enumerate(runs):
            _head(db, run).last_dispatched_at = now - timedelta(hours=2 - index)
        scopes, heads = [], []
        for i in range(130):
            identity = UUID(int=i + 1)
            scopes.append(
                dict(scope.model_dump(), id=identity, identity_digest=f"{i:064x}")
            )
            heads.append(
                dict(
                    first.model_dump(),
                    scope_id=identity,
                    last_dispatched_at=None,
                    oldest_pending_at=now - timedelta(days=2),
                )
            )
        db.execute(SyncScope.__table__.insert(), scopes)
        db.execute(SyncSchedule.__table__.insert(), heads)
    counts = []

    def count_pages(_connection, _cursor, statement, parameters, _context, _many):
        if (
            "picked.id" in statement
            and isinstance(parameters, dict)
            and parameters.get("work_class") == "directory"
        ):
            counts.append(statement)

    with factory() as db:
        engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count_pages)
    try:
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db, redis_client, now=now, limit=40, max_claims=1
                )
                == 1
            )
        first_pages = len(counts)
        counts.clear()
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=now + timedelta(seconds=1),
                    limit=40,
                    max_claims=1,
                )
                == 1
            )
        assert first_pages >= 3 and len(counts) == 1
    finally:
        event.remove(engine, "before_cursor_execute", count_pages)
