"""canonical到期头只读取当前执行，历史正文与outbox不参与公平排名。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlmodel import select

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from tests.modules.reporting.directory_claiming import finish, pending, sent
from tests.modules.sync_control.conftest import (
    isolated_sync_quota as isolated_sync_quota,
)


def test_locked_first_candidate_does_not_leave_a_free_slot(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        first = pending(db, seed, age=2).id
        second = pending(db, seed, advertiser="acc-2").id
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == first).with_for_update()
        ).one()
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=1,
                )
                == 1
            )
            assert [r.id for r in sent(db)] == [second]


def _visited(plan, relation):
    own = 0
    if plan.get("Relation Name") == relation:
        own = (
            plan.get("Actual Rows", 0) + plan.get("Rows Removed by Filter", 0)
        ) * plan.get("Actual Loops", 1)
    return own + sum(_visited(child, relation) for child in plan.get("Plans", []))


def test_locked_dependency_heads_preserve_background_progress(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        locked = pending(db, seed, "identity", background=True, age=30).id
        full = pending(
            db, seed, "identity", advertiser="acc-2", background=True, full=True, age=30
        ).id
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == locked).with_for_update()
        ).one()
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=1,
                )
                == 1
            )
            assert [r.id for r in sent(db)] == [full]


def test_large_live_lane_does_not_starve_other_scope_or_tenant(
    session, sync_seed, redis_client
):
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    other = seed_fixture.__wrapped__(session)
    for _ in range(32):
        pending(session, sync_seed, age=60)
    historical = pending(
        session, sync_seed, "identity", advertiser="acc-2", background=True
    )
    neighbor = pending(session, other, "identity", background=True)
    delivered = set()
    for _ in range(8):
        assert (
            dispatch_directories_due(
                session,
                redis_client,
                now=datetime.now(UTC) + timedelta(seconds=1),
                limit=1,
            )
            == 1
        )
        active = [r for r in sent(session) if r.status != "COMPLETE"]
        assert len(active) == 1
        delivered.add(active[0].id)
        finish(session, redis_client, active[0], now=datetime.now(UTC))
        if {historical.id, neighbor.id} <= delivered:
            break
    assert {historical.id, neighbor.id} <= delivered


def test_history_is_not_rescanned_for_each_head_and_capacity(
    session, sync_seed, redis_client, record_property
):
    # bounded local fixture: new contract must do *less* than old one-pass 30k scan.
    # Five thousand terminal runs and fifteen thousand historical envelopes are enough
    # to reject a full-history traversal without an unbounded expensive seed.
    history = pending(session, sync_seed, advertiser="acc-2")
    history.status = "COMPLETE"
    history.published_version = 1
    from app.modules.sync_control.directory_demand import directory_outcome

    directory_outcome(session, history)
    session.flush()
    session.execute(
        text("""
      INSERT INTO ad_directory_run OVERRIDING SYSTEM VALUE
      SELECT (jsonb_populate_record(NULL::ad_directory_run,to_jsonb(r)||jsonb_build_object(
        'id',gen_random_uuid(),'request_id',gen_random_uuid(),
        'request_sequence',nextval(pg_get_serial_sequence('ad_directory_run','request_sequence')),
        'partition_key',lpad(n::text,64,'0')))).*
      FROM ad_directory_run r CROSS JOIN generate_series(1,5000)n WHERE r.id=:id
    """),
        {"id": history.id},
    )
    session.execute(
        text("""
      INSERT INTO pending_dispatch(id,tenant_id,actor_id,task_name,task_key,payload,
        available_at,published_at,attempts)
      SELECT gen_random_uuid(),:tenant,:actor,'ads.targeted_step','old-directory:'||n,
        '{}'::jsonb,:now,:now,1 FROM generate_series(1,15000)n
    """),
        {
            "tenant": sync_seed.tenant_id,
            "actor": sync_seed.actor_id,
            "now": datetime.now(UTC),
        },
    )
    targets = [pending(session, sync_seed, advertiser=f"due-{i}") for i in range(2)]
    session.execute(text("ANALYZE ad_directory_run"))
    session.execute(text("ANALYZE pending_dispatch"))
    plans = []

    def capture(_conn, cursor, sql, params, _context, _many):
        if sql.lstrip().upper().startswith(("SELECT", "WITH")) and (
            "ad_directory_run" in sql or "pending_dispatch" in sql
        ):
            cursor.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, params)
            plans.append(cursor.fetchone()[0][0])

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", capture)
    try:
        assert (
            dispatch_directories_due(
                session,
                redis_client,
                now=datetime.now(UTC) + timedelta(seconds=1),
                limit=2,
            )
            == 2
        )
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    from uuid import UUID

    from app.jobs.models import PendingDispatch

    delivered = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name.in_(
                ["ads.sync_step", "ads.targeted_step", "ads.publish_step"]
            ),
            PendingDispatch.published_at.is_(None),
        )
    ).all()
    assert {UUID(p.payload["run_id"]) for p in delivered} == {r.id for r in targets}
    visits = {
        name: sum(_visited(p["Plan"], name) for p in plans)
        for name in ("ad_directory_run", "pending_dispatch")
    }
    record_property("directory_dispatch_plans", json.dumps(plans))
    record_property("history_visits", json.dumps(visits))
    assert visits["ad_directory_run"] < 500 and visits["pending_dispatch"] < 100, visits
    assert all(p["Plan"].get("Temp Written Blocks", 0) == 0 for p in plans)


def test_pending_30k_and_outbox_90k_are_not_repeatedly_scanned(
    session, sync_seed, redis_client, record_property
):
    """保留原30k活动规模；批量复制正式登记形状，不能以终态历史替代。"""
    from app.modules.sync_control.models import SyncExecutionScope

    templates = [
        pending(session, sync_seed, service) for service in ("status", "identity")
    ]
    children = [
        session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.directory_run_id == run.id
            )
        ).one()
        for run in templates
    ]
    assert children[0].scope_id == children[1].scope_id
    params = {
        "run": templates[0].id,
        "child": children[0].id,
        "tenant": sync_seed.tenant_id,
        "actor": sync_seed.actor_id,
        "now": datetime.now(UTC),
    }
    session.execute(
        text("""CREATE TEMPORARY TABLE directory_cost_seed ON COMMIT DROP AS
        SELECT n,gen_random_uuid() AS run_id,gen_random_uuid() AS requirement_id
        FROM generate_series(1,30000)n""")
    )
    session.execute(
        text("""
      INSERT INTO ad_directory_run OVERRIDING SYSTEM VALUE
      SELECT (jsonb_populate_record(NULL::ad_directory_run,to_jsonb(r)||jsonb_build_object(
        'id',x.run_id,'request_id',gen_random_uuid(),
        'request_sequence',nextval(pg_get_serial_sequence('ad_directory_run','request_sequence')),
        'partition_key',lpad(x.n::text,64,'0'),
        'workload',CASE WHEN x.n%2=0 THEN 'live' ELSE 'background' END,
        'query',r.query||jsonb_build_object('ids',jsonb_build_array(x.n::text),
            'directory_service_v1',CASE WHEN x.n%4=0 THEN 'status' ELSE 'identity' END)))).*
      FROM ad_directory_run r CROSS JOIN directory_cost_seed x WHERE r.id=:run
    """),
        params,
    )
    session.execute(
        text("""
      INSERT INTO sync_requirement
      SELECT (jsonb_populate_record(NULL::sync_requirement,to_jsonb(q)||jsonb_build_object(
        'id',x.requirement_id,'bucket_key',r.partition_key,
        'bucket',jsonb_build_object('key',r.partition_key,'query',r.query),
        'source',CASE WHEN x.n%4=0 THEN 'realtime' ELSE 'directory' END))).*
      FROM sync_execution_scope e JOIN sync_requirement q ON q.id=e.requirement_id
      CROSS JOIN directory_cost_seed x JOIN ad_directory_run r ON r.id=x.run_id
      WHERE e.id=:child
    """),
        params,
    )
    session.execute(
        text("""
      INSERT INTO sync_execution_scope
      SELECT (jsonb_populate_record(NULL::sync_execution_scope,to_jsonb(e)||jsonb_build_object(
        'id',gen_random_uuid(),'requirement_id',x.requirement_id,'directory_run_id',x.run_id))).*
      FROM sync_execution_scope e CROSS JOIN directory_cost_seed x WHERE e.id=:child
    """),
        params,
    )
    session.execute(
        text("""
      INSERT INTO pending_dispatch(id,tenant_id,actor_id,task_name,task_key,payload,available_at,published_at,attempts)
      SELECT gen_random_uuid(),:tenant,:actor,'ads.targeted_step','historical:'||n,
        '{}'::jsonb,:now,:now,1 FROM generate_series(1,90000)n
    """),
        params,
    )
    for table in (
        "ad_directory_run",
        "pending_dispatch",
        "sync_execution_scope",
        "sync_requirement",
        "sync_schedule",
        "sync_scope",
    ):
        session.execute(text(f"ANALYZE {table}"))
    plans = []

    def capture(_conn, cursor, sql, params, _context, _many):
        if sql.lstrip().upper().startswith(("SELECT", "WITH")) and (
            "ad_directory_run" in sql or "pending_dispatch" in sql
        ):
            cursor.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, params)
            plans.append(cursor.fetchone()[0][0])

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", capture)
    try:
        dispatched = dispatch_directories_due(
            session, redis_client, now=datetime.now(UTC) + timedelta(seconds=1), limit=8
        )
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    assert dispatched == 2  # 同scope两工作类各最多一个领取，不能重回旧40请求窗口。
    visits = {
        name: sum(_visited(p["Plan"], name) for p in plans)
        for name in ("ad_directory_run", "pending_dispatch")
    }
    record_property("directory_dispatch_plans", json.dumps(plans))
    record_property("history_visits", json.dumps(visits))
    assert visits["ad_directory_run"] <= 31000, visits
    assert visits["pending_dispatch"] < 100, visits
    assert all(p["Plan"].get("Temp Written Blocks", 0) == 0 for p in plans)


@pytest.mark.parametrize("locked_table", ["run", "execution", "requirement"])
def test_same_head_skips_locked_old_candidate_before_limit(
    committed_sync_seed, redis_client, locked_table
):
    from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        first = pending(db, seed, age=2).id
        second = pending(db, seed, age=1).id
        ref = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.directory_run_id == first
            )
        ).one()
        identity = {
            "run": first,
            "execution": ref.id,
            "requirement": ref.requirement_id,
        }[locked_table]
    model = {
        "run": AdDirectoryRun,
        "execution": SyncExecutionScope,
        "requirement": SyncRequirement,
    }[locked_table]
    with factory() as blocker, blocker.begin():
        blocker.get(model, identity, with_for_update=True)
        with factory() as db, db.begin():
            assert (
                dispatch_directories_due(
                    db,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=1,
                )
                == 1
            )
            assert [r.id for r in sent(db)] == [second]
