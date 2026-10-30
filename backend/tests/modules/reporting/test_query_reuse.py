from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.test_material_query import D


def test_twenty_same_actor_queries_build_once(committed_sync_seed):
    from app.core.db import engine
    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.query_reuse import find_or_build_snapshot

    seed, _factory = committed_sync_seed
    filters = ReportingFilter(dimension="account", start_date=D, end_date=D)
    count = 0
    lock = Lock()

    def build(*args, **kwargs):
        nonlocal count
        with lock:
            count += 1
        return _build_snapshot(*args, **kwargs)

    def query(_index):
        return find_or_build_snapshot(
            engine, context=seed.context, bc_id="bc-a", filters=filters, builder=build
        ).id

    with ThreadPoolExecutor(max_workers=20) as executor:
        ids = list(executor.map(query, range(20)))
    assert len(set(ids)) == 1
    assert count == 1


def test_expired_builder_is_replaced_and_stale_token_cannot_publish(
    committed_sync_seed,
):
    from app.core.db import engine
    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.query_reuse import (
        claim_build,
        complete_build,
        find_or_build_snapshot,
        query_key,
    )

    seed, factory = committed_sync_seed
    filters = ReportingFilter(dimension="account", start_date=D, end_date=D)
    key = query_key(seed.context, "bc-a", filters)
    with factory() as session:
        stale = claim_build(
            session, key=key, context=seed.context, bc_id="bc-a", lease_seconds=-1
        )
        session.commit()
    current = find_or_build_snapshot(
        engine,
        context=seed.context,
        bc_id="bc-a",
        filters=filters,
        builder=_build_snapshot,
    )
    with factory() as session:
        assert not complete_build(session, key=key, claim=stale, snapshot=current)


def test_reuse_dependency_changes_when_nonmax_account_bucket_changes(report_case):
    from app.modules.reporting.query_reuse import dependency_stamp
    from app.modules.reporting.read_models.dirty_buckets import (
        ReadBucketKey,
        mark_dirty,
    )
    from tests.modules.reporting.test_queries import filters_for

    case = report_case
    case.seed_campaign("P-Drama", "report-account", 1, 2)
    first = dependency_stamp(
        case.session, context=case.context, bc_id=case.bc_id, filters=filters_for()
    )
    mark_dirty(
        case.session,
        [
            ReadBucketKey(
                case.context.tenant_id, "report-account", "ad_day", case.fact_date
            )
        ],
    )
    second = dependency_stamp(
        case.session, context=case.context, bc_id=case.bc_id, filters=filters_for()
    )
    assert first.digest != second.digest


def test_last_waiter_cancels_sql_but_one_disconnect_does_not_cancel_other_waiters(
    committed_sync_seed,
):
    from concurrent.futures import TimeoutError

    from sqlalchemy.exc import DBAPIError

    from app.core.db import engine
    from app.modules.reporting.query_reuse import (
        cancel_waiter,
        find_or_build_snapshot,
        query_key,
        register_waiter,
    )

    seed, factory = committed_sync_seed
    filters = ReportingFilter(dimension="account", start_date=D, end_date=D)
    key = query_key(seed.context, "bc-a", filters)
    owner, other = uuid4(), uuid4()
    entered = Event()

    def slow_builder(session, **_kwargs):
        entered.set()
        session.connection().execute(text("SELECT pg_sleep(30)"))
        raise AssertionError("SQL should have been cancelled")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            find_or_build_snapshot,
            engine,
            context=seed.context,
            bc_id="bc-a",
            filters=filters,
            builder=slow_builder,
            waiter_id=owner,
        )
        assert entered.wait(5)
        with factory() as session:
            register_waiter(
                session, key=key, waiter_id=other, context=seed.context, bc_id="bc-a"
            )
            session.commit()
        assert not cancel_waiter(engine, key=key, waiter_id=owner)
        with pytest.raises(TimeoutError):
            future.result(timeout=0.05)
        assert cancel_waiter(engine, key=key, waiter_id=other)
        with pytest.raises(DBAPIError) as error:
            future.result(timeout=3)
        assert error.value.orig.sqlstate == "57014"


def test_snapshot_builder_never_holds_one_pool_slot_while_requesting_another(
    committed_sync_seed,
):
    """并发冷构建不能占满池后各自等待第二条租约登记连接。"""
    from sqlalchemy import create_engine

    from app.core.db import engine
    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.query_reuse import find_or_build_snapshot

    seed, _factory = committed_sync_seed
    constrained = create_engine(
        engine.url, pool_size=1, max_overflow=0, pool_timeout=0.1
    )
    try:
        snapshot = find_or_build_snapshot(
            constrained,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(dimension="account", start_date=D, end_date=D),
            builder=_build_snapshot,
        )
        assert snapshot.id
        assert constrained.pool.checkedout() == 0
    finally:
        constrained.dispose()


def test_rr_builder_serialization_after_takeover_reuses_winner(
    committed_sync_seed, monkeypatch
):
    from sqlalchemy import event

    from app.core.db import engine
    from app.modules.reporting import query_reuse
    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.query_models import QuerySnapshot

    seed, factory = committed_sync_seed
    filters = ReportingFilter(dimension="account", start_date=D, end_date=D)
    original_claim = query_reuse.claim_build
    calls = 0
    started, release = Event(), Event()
    old_ids, states = [], []

    def short_first_lease(session, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            # B先锁住争用行，A在租约仍有效时进入UPDATE并等待B；B等自然
            # 到期后接管提交，迫使A面对真实RR concurrent-update 40001。
            session.execute(
                text("SELECT key FROM query_snapshot_build WHERE key=:key FOR UPDATE"),
                {"key": kwargs["key"]},
            )
            release.set()
            session.execute(text("SELECT pg_sleep(1.1)"))
        return original_claim(session, **kwargs, lease_seconds=1 if calls == 1 else 45)

    def observe_error(context):
        states.append(getattr(context.original_exception, "sqlstate", None))

    def stale_builder(session, **kwargs):
        snapshot = _build_snapshot(session, **kwargs)
        old_ids.append(snapshot.id)
        started.set()
        assert release.wait(5)
        return snapshot

    monkeypatch.setattr(query_reuse, "claim_build", short_first_lease)
    event.listen(engine, "handle_error", observe_error)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                query_reuse.find_or_build_snapshot,
                engine,
                context=seed.context,
                bc_id="bc-a",
                filters=filters,
                builder=stale_builder,
            )
            try:
                assert started.wait(3)
                winner = query_reuse.find_or_build_snapshot(
                    engine,
                    context=seed.context,
                    bc_id="bc-a",
                    filters=filters,
                    builder=_build_snapshot,
                )
            finally:
                release.set()
            assert first.result(timeout=3).id == winner.id
        assert "40001" in states  # 真实RR更新冲突，不是模拟返回False。
        with factory() as session:
            assert session.get(QuerySnapshot, old_ids[0]) is None
            assert session.get(QuerySnapshot, winner.id) is not None
    finally:
        event.remove(engine, "handle_error", observe_error)


def test_build_sql_uses_remaining_request_budget(committed_sync_seed):
    from time import monotonic

    from fastapi import HTTPException

    from app.core.db import engine
    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.query_reuse import find_or_build_snapshot

    seed, _factory = committed_sync_seed

    def slow(session, **kwargs):
        session.execute(text("SELECT pg_sleep(1)"))
        return _build_snapshot(session, **kwargs)

    started = monotonic()
    with pytest.raises(HTTPException) as error:
        find_or_build_snapshot(
            engine,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(dimension="account", start_date=D, end_date=D),
            builder=slow,
            wait_seconds=0.2,
        )
    assert error.value.status_code == 503
    assert monotonic() - started < 0.9


def test_serialization_retries_are_bounded_after_rollback(committed_sync_seed):
    from fastapi import HTTPException

    from app.core.db import engine
    from app.modules.reporting.query_reuse import find_or_build_snapshot

    seed, _factory = committed_sync_seed
    calls = 0

    def fail_in_postgres(session, **_kwargs):
        nonlocal calls
        calls += 1
        session.execute(
            text(
                "DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='40001', MESSAGE='test retry budget'; END $$"
            )
        )
        raise AssertionError("unreachable")

    with pytest.raises(HTTPException) as error:
        find_or_build_snapshot(
            engine,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(dimension="account", start_date=D, end_date=D),
            builder=fail_in_postgres,
        )
    assert error.value.status_code == 503
    assert calls == 3


def test_slow_build_ends_before_lease_and_does_not_restart(
    committed_sync_seed, monkeypatch
):
    from fastapi import HTTPException

    from app.core.db import engine
    from app.modules.reporting import query_reuse

    seed, _factory = committed_sync_seed
    calls = 0
    monkeypatch.setattr(query_reuse, "MAX_BUILD_SECONDS", 0.2)

    def slow(session, **_kwargs):
        nonlocal calls
        calls += 1
        session.execute(text("SELECT pg_sleep(1)"))
        raise AssertionError("budget should interrupt SQL")

    with pytest.raises(HTTPException) as error:
        query_reuse.find_or_build_snapshot(
            engine,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(dimension="account", start_date=D, end_date=D),
            builder=slow,
            wait_seconds=5,
        )
    assert error.value.status_code == 503
    assert calls == 1
    assert query_reuse.BUILD_LEASE_SECONDS > query_reuse.MAX_BUILD_SECONDS


@pytest.mark.parametrize("has_other_waiter", [False, True])
def test_cancel_after_application_tag_releases_single_pool_slot(
    committed_sync_seed, has_other_waiter
):
    """取消恰好落在pid/tag登记窗口时，不得占着RR连接再申请控制连接。"""
    from fastapi import HTTPException
    from sqlalchemy import create_engine, event

    from app.core.db import engine
    from app.modules.reporting import query_reuse
    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.query_models import QuerySnapshotWaiter

    seed, factory = committed_sync_seed
    constrained = create_engine(
        engine.url, pool_size=1, max_overflow=0, pool_timeout=0.1
    )
    filters = ReportingFilter(dimension="account", start_date=D, end_date=D)
    key = query_reuse.query_key(seed.context, "bc-a", filters)
    other = uuid4()
    tagged, release, cancelled = Event(), Event(), Event()
    if has_other_waiter:
        with factory() as session:
            query_reuse.register_waiter(
                session, key=key, waiter_id=other, context=seed.context, bc_id="bc-a"
            )
            session.commit()

    def barrier(_conn, _cursor, sql, _params, _context, _many):
        if "set_config('application_name'" in sql and not tagged.is_set():
            tagged.set()
            assert release.wait(3)

    event.listen(constrained, "after_cursor_execute", barrier)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                query_reuse.find_or_build_snapshot,
                constrained,
                context=seed.context,
                bc_id="bc-a",
                filters=filters,
                builder=_build_snapshot,
                cancelled=cancelled.is_set,
            )
            assert tagged.wait(3)
            cancelled.set()
            release.set()
            with pytest.raises(HTTPException) as error:
                future.result(timeout=3)
            assert error.value.status_code == 499
        assert constrained.pool.checkedout() == 0
        if has_other_waiter:
            with factory() as session:
                assert session.get(QuerySnapshotWaiter, other) is not None
            # 原消费者仍可接管并完成；取消者只释放本代，不能删除其他等待者。
            snapshot = query_reuse.find_or_build_snapshot(
                constrained,
                context=seed.context,
                bc_id="bc-a",
                filters=filters,
                builder=_build_snapshot,
            )
            assert snapshot.id
    finally:
        release.set()
        event.remove(constrained, "after_cursor_execute", barrier)
        constrained.dispose()
