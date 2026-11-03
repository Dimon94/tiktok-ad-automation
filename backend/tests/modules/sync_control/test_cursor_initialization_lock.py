"""最后合并游标时跳过锁与初始化竞争，准备阶段只锁对应接口。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text

from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.dispatch_budget import (
    DispatchBudgetConflict,
    EndpointBudgets,
)


def _prepared(session, tenant_id, endpoint="test"):
    budgets = EndpointBudgets(session)
    cursor = budgets.acquire(tenant_id, endpoint)
    assert cursor is not None
    cursor.last_dispatched_at = datetime.now(UTC) + timedelta(seconds=1)
    return budgets


def test_updated_cursor_is_skipped_before_initialization_insert(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        _prepared(db, seed.tenant_id).persist()
    with factory() as owner, owner.begin():
        cursor = owner.get(ReportDispatchCursor, seed.tenant_id, with_for_update=True)
        cursor.last_dispatched_at = datetime.now(UTC) + timedelta(seconds=1)
        cursor.fairness_state = {"test": {"realtime": 1}}
        owner.flush()
        with factory() as contender, contender.begin():
            contender.execute(text("SET LOCAL lock_timeout='100ms'"))
            prepared = _prepared(contender, seed.tenant_id)
            with pytest.raises(DispatchBudgetConflict):
                prepared.persist()
    with factory() as db, db.begin():
        _prepared(db, seed.tenant_id).persist()


def test_concurrent_first_cursor_creation_does_not_wait_on_unique_key(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as owner, owner.begin():
        _prepared(owner, seed.tenant_id, "one").persist()
        with factory() as contender, contender.begin():
            contender.execute(text("SET LOCAL lock_timeout='100ms'"))
            prepared = _prepared(contender, seed.tenant_id, "two")
            with pytest.raises(DispatchBudgetConflict):
                prepared.persist()
    with factory() as db, db.begin():
        _prepared(db, seed.tenant_id, "two").persist()
        assert set(db.get(ReportDispatchCursor, seed.tenant_id).fairness_state) == {
            "one",
            "two",
        }


def test_same_endpoint_is_serial_but_other_endpoint_can_prepare(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as owner, owner.begin():
        assert EndpointBudgets(owner).acquire(seed.tenant_id, "one") is not None
        with factory() as contender, contender.begin():
            budgets = EndpointBudgets(contender)
            assert budgets.acquire(seed.tenant_id, "one") is None
            assert budgets.acquire(seed.tenant_id, "two") is not None


def test_existing_unlocked_cursor_needs_no_initialization_write(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        _prepared(db, seed.tenant_id).persist()
    statements = []
    with factory() as db, db.begin():

        def capture(_conn, _cursor, sql, _params, _context, _many):
            statements.append(sql)

        connection = db.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            _prepared(db, seed.tenant_id).persist()
        finally:
            event.remove(connection, "before_cursor_execute", capture)
    assert not any("INSERT INTO report_dispatch_cursor" in sql for sql in statements)
