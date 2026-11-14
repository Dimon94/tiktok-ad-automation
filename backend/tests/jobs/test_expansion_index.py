"""Real service SQL must retain its partial-index proof in prepared plans."""

# ruff: noqa: F811
import re

from alembic import command
from psycopg import sql
from sqlalchemy import event, inspect, text

from tests.jobs.test_expansion_fairness import (
    enqueue,
    queues,  # noqa: F401
)
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database,  # noqa: F401
)

INDEX = "ix_dispatch_expansion_pending"


def capture_expansion_queries(engine, captured):
    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if (
            statement.startswith("SELECT")
            and "pending_dispatch.task_name =" in statement
        ):
            captured.append((statement, parameters.copy()))

    event.listen(engine, "before_cursor_execute", capture)
    return lambda: event.remove(engine, "before_cursor_execute", capture)


def assert_prepared_index(engine, captured, tenant_id):
    """Use the exact emitted service SQL, with tenant/time still parameterized."""
    statement, parameters = captured
    assert "'builds.expand_submission'" in statement
    assert "builds.expand_submission" not in parameters.values()
    assert tenant_id in parameters.values()
    names = list(dict.fromkeys(re.findall(r"%\((\w+)\)s", statement)))
    prepared = re.sub(
        r"%\((\w+)\)s", lambda match: f"${names.index(match[1]) + 1}", statement
    )
    with engine.connect() as connection:
        # An old ordinary fanout precedes the rare expansion in the same tenant.
        connection.execute(
            text("""
                INSERT INTO pending_dispatch
                    (id, tenant_id, actor_id, task_name, task_key, payload,
                     available_at, attempts)
                SELECT gen_random_uuid(), :tenant, :tenant,
                       'builds.execute_unit', 'index-probe-' || n,
                       '{}'::jsonb, now() - interval '1 day', 0
                FROM generate_series(1, 20000) n
            """),
            {"tenant": tenant_id},
        )
        connection.exec_driver_sql("ANALYZE pending_dispatch")
        raw = connection.connection.driver_connection
        with raw.cursor() as cursor:
            for mode in ("force_generic_plan", "auto"):
                cursor.execute(
                    sql.SQL("SET LOCAL plan_cache_mode = {}").format(sql.SQL(mode))
                )
                cursor.execute(sql.SQL("PREPARE expansion_probe AS " + prepared))
                execute = sql.SQL("EXECUTE expansion_probe ({})").format(
                    sql.SQL(", ").join(sql.Literal(parameters[name]) for name in names)
                )
                for _ in range(10):
                    cursor.execute(execute)
                    cursor.fetchall()
                cursor.execute(
                    sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ") + execute
                )
                plan = cursor.fetchone()[0][0]["Plan"]

                def indexes(node):
                    return [node.get("Index Name")] + [
                        index
                        for child in node.get("Plans", [])
                        for index in indexes(child)
                    ]

                assert INDEX in indexes(plan)
                assert plan.get("Shared Hit Blocks", 0) < 100
                assert plan.get("Shared Read Blocks", 0) < 100
                cursor.execute(
                    "SELECT generic_plans FROM pg_prepared_statements WHERE name = 'expansion_probe'"
                )
                assert cursor.fetchone()[0] > 0
                cursor.execute("DEALLOCATE expansion_probe")
        connection.rollback()


def test_actual_priority_query_uses_partial_index_after_prepared_warmup(queues):
    from sqlmodel import Session

    from app.jobs import outbox
    from app.jobs.models import PendingDispatch

    db, first, _, _ = queues
    with Session(db) as session, session.begin():
        identity = enqueue(session, first, expansion=True)
    captured = []
    remove = capture_expansion_queries(db, captured)
    try:
        assert outbox.flush_dispatch() == 1
    finally:
        remove()
    with Session(db) as session, session.begin():
        row = session.get(PendingDispatch, identity)
        row.published_at = None
        session.add(row)
    assert len(captured) == 1
    assert_prepared_index(db, captured[0], first.tenant_id)


def test_expansion_index_fresh_roundtrip_matches_model(monkeypatch):
    from tests.migration_database import historical_database

    with historical_database(monkeypatch, "0012_material_covers") as (db, config):

        def indexes():
            return {
                row["name"]: row for row in inspect(db).get_indexes("pending_dispatch")
            }

        assert INDEX not in indexes()
        with db.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO pending_dispatch
                    (id, tenant_id, actor_id, task_name, task_key, payload, available_at, attempts)
                VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
                        'builds.expand_submission', 'historical-expansion', '{}'::jsonb, now(), 0)
            """)
            )
        command.upgrade(config, "0013_dispatch_expansion_index")
        actual = indexes()[INDEX]
        assert actual["column_names"] == ["tenant_id", "available_at", "id"]
        predicate = actual["dialect_options"]["postgresql_where"]
        assert "published_at IS NULL" in predicate
        assert "builds.expand_submission" in predicate
        command.downgrade(config, "0012_material_covers")
        assert INDEX not in indexes()
        command.upgrade(config, "0013_dispatch_expansion_index")
        assert indexes()[INDEX] == actual
        command.upgrade(config, "head")
        current = indexes()[INDEX]
        assert current["column_names"] == actual["column_names"]
        predicate = current["dialect_options"]["postgresql_where"]
        # 今日待投递集合排除已替代和退役消息；0013 的往返定义仍在上面核对。
        for clause in (
            "published_at IS NULL",
            "superseded_at IS NULL",
            "retired_at IS NULL",
            "builds.expand_submission",
        ):
            assert clause in predicate
        with db.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pending_dispatch WHERE task_key='historical-expansion'"
                    )
                ).scalar_one()
                == 1
            )
        command.check(config)
