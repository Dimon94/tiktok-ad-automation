"""到期scope/需求反查不能随无关执行历史线性扫描。"""

import json
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.sync_control.models import SyncExecutionScope
from tests.modules.reporting.test_jobs import _route


def test_scope_and_requirement_lookup_skip_ten_thousand_other_children(
    session, sync_seed
):
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "history",
        "start": date(2026, 10, 1),
        "end": date(2026, 10, 1),
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    first = enqueue_job(session, advertiser_ids=["acc-1"], **common)
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == first)
    ).one()
    session.execute(
        text("""
      WITH requirements AS (
        INSERT INTO sync_requirement(id,tenant_id,scope_id,bucket_key,bucket,desired_revision,
          satisfied_revision,requested_after,oldest_pending_at,source)
        SELECT gen_random_uuid(),:tenant,:scope,'cost-'||n,'{}'::jsonb,1,0,:now,:now,'history'
        FROM generate_series(1,10000) n RETURNING id
      )
      INSERT INTO sync_execution_scope(id,tenant_id,scope_id,requirement_id,report_job_id,
        frozen_revision,frozen_requested_after,owner_epoch,execution_generation,status)
      SELECT gen_random_uuid(),:tenant,:scope,id,:job,1,:now,1,0,'PENDING' FROM requirements
    """),
        {
            "tenant": sync_seed.tenant_id,
            "scope": child.scope_id,
            "job": first,
            "now": datetime.now(UTC),
        },
    )
    target = enqueue_job(session, advertiser_ids=["acc-2"], **common)
    target_child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == target)
    ).one()
    session.execute(text("ANALYZE sync_execution_scope"))
    plans = {}
    for name, sql, params in (
        (
            "scope",
            "SELECT id FROM sync_execution_scope WHERE scope_id=:scope AND status='PENDING' LIMIT 1",
            {"scope": target_child.scope_id},
        ),
        (
            "requirement",
            "SELECT id FROM sync_execution_scope WHERE requirement_id=:requirement",
            {"requirement": target_child.requirement_id},
        ),
    ):
        result = session.execute(
            text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params
        ).scalar_one()[0]
        plans[name] = result
    print("SYNC_LOOKUP_PLANS " + json.dumps(plans, default=str))  # noqa: T201
    assert all(
        plan["Plan"].get("Shared Hit Blocks", 0) < 100 for plan in plans.values()
    ), plans


def test_planning_timezones_do_not_repeat_tenant_prefix_for_every_account(
    session, sync_seed
):
    from sqlalchemy import event

    from app.modules.reporting import planner
    from app.modules.reporting.store_models import AdSyncBc

    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    session.execute(
        text("""
      INSERT INTO advertiser_account(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
      SELECT :tenant,'metadata-'||n,'','USD','UTC','UNKNOWN',false FROM generate_series(1,5000)n
    """),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(
        text("""
      INSERT INTO ad_sync_account(tenant_id,advertiser_id,owner_bc_id)
      SELECT :tenant,'metadata-'||n,'bc-a' FROM generate_series(1,5000)n
    """),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(text("ANALYZE advertiser_account"))
    session.execute(text("ANALYZE ad_sync_account"))
    connection = session.connection()
    captured = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "planning_owned" in statement:
            captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        values = planner.owned_account_timezones(
            session, session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert all(values[f"metadata-{index}"] == "UTC" for index in range(1, 5001))
    sql, params = captured[-1]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params
    ).scalar_one()[0]
    print("SYNC_TIMEZONE_PLAN " + json.dumps(plan, default=str))  # noqa: T201
    assert plan["Plan"].get("Shared Hit Blocks", 0) < 2000, plan


def test_shared_pool_due_probes_skip_large_terminal_execution_history(
    session, sync_seed
):
    """十万已结束child不能成为每次公共池机会探测的扫描基数。"""
    from sqlalchemy import event

    from app.modules.sync_control.worker_pool import (
        _due_categories,
        background_lease_count,
    )

    now = datetime.now(UTC)
    job = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        kind="ad_day",
        tier="history",
        advertiser_ids=["acc-1"],
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
        frozen_route=_route(sync_seed),
        due_at=now,
    )
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job)
    ).one()
    session.execute(
        text("""
      WITH old AS (
        INSERT INTO sync_requirement(id,tenant_id,scope_id,bucket_key,bucket,desired_revision,
          satisfied_revision,requested_after,oldest_pending_at,source)
        SELECT gen_random_uuid(),:tenant,:scope,'old-cost-'||n,'{}'::jsonb,1,1,:now,:now,'history'
        FROM generate_series(1,100000)n RETURNING id
      ) INSERT INTO sync_execution_scope(id,tenant_id,scope_id,requirement_id,report_job_id,
        frozen_revision,frozen_requested_after,owner_epoch,execution_generation,status)
      SELECT gen_random_uuid(),:tenant,:scope,id,:job,1,:now,1,0,'PUBLISHED' FROM old
    """),
        {
            "tenant": sync_seed.tenant_id,
            "scope": child.scope_id,
            "job": job,
            "now": now,
        },
    )
    session.execute(text("ANALYZE sync_execution_scope"))
    session.execute(text("ANALYZE sync_schedule"))
    session.execute(text("ANALYZE report_sync_job"))
    connection, captured = session.connection(), []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "SELECT 'facts'" in statement or "WITH leased_heads" in statement:
            captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert _due_categories(session, now=now, allow_backfill=True) == {"facts"}
        assert background_lease_count(session, now=now) == 0
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    plans = []
    for statement, parameters in captured:
        plan = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, parameters
        ).scalar_one()[0]
        plans.append(plan)
    print("SYNC_WORKER_POOL_HISTORY_PLANS " + json.dumps(plans, default=str))  # noqa:T201
    assert len(plans) == 2
    assert all(plan["Plan"].get("Shared Hit Blocks", 0) < 500 for plan in plans), plans
