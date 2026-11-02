"""已存在且被更新的租户游标必须先跳锁，不能被初始化UPSERT阻塞。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.fairness import lock_tenant_cursor


def test_updated_cursor_is_skipped_before_initialization_insert(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        assert lock_tenant_cursor(db, seed.tenant_id) is not None
    with factory() as owner, owner.begin():
        cursor = owner.get(ReportDispatchCursor, seed.tenant_id, with_for_update=True)
        cursor.last_dispatched_at = datetime.now(UTC) + timedelta(seconds=1)
        cursor.fairness_state = {"test": {"realtime": 1}}
        owner.flush()
        with factory() as contender, contender.begin():
            contender.execute(text("SET LOCAL lock_timeout='100ms'"))
            assert lock_tenant_cursor(contender, seed.tenant_id) is None
    with factory() as db, db.begin():
        assert lock_tenant_cursor(db, seed.tenant_id) is not None


def test_concurrent_first_cursor_creation_does_not_wait_on_unique_key(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as owner, owner.begin():
        assert lock_tenant_cursor(owner, seed.tenant_id) is not None
        with factory() as contender, contender.begin():
            contender.execute(text("SET LOCAL lock_timeout='100ms'"))
            assert lock_tenant_cursor(contender, seed.tenant_id) is None
    with factory() as db, db.begin():
        assert lock_tenant_cursor(db, seed.tenant_id) is not None


def test_existing_unlocked_cursor_needs_no_initialization_write(committed_sync_seed):
    from sqlalchemy import event

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        assert lock_tenant_cursor(db, seed.tenant_id) is not None
    statements = []
    with factory() as db, db.begin():

        def capture(_conn, _cursor, sql, _params, _context, _many):
            statements.append(sql)

        connection = db.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            assert lock_tenant_cursor(db, seed.tenant_id) is not None
        finally:
            event.remove(connection, "before_cursor_execute", capture)
    assert not any("INSERT INTO report_dispatch_cursor" in sql for sql in statements)
