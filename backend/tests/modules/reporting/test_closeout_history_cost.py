"""收尾历史按冻结范围和日期读取；所有终态仍阻止重复规划。"""

import json
import re
from datetime import timedelta
from math import ceil
from uuid import uuid4

import pytest
from psycopg import sql as pgsql
from sqlalchemy import event, text

from app.modules.reporting import planner
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_day_closeout import NOW, plan_for, seed_yesterday


def _capture_history(session, plan):
    captured = []

    class Captured(Exception):
        pass

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "j.start_date=j.end_date" in statement:
            captured.append((statement, parameters))
            raise Captured()

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        with pytest.raises(Captured):
            planner.plan_day_closeout(session, plan, now=NOW, accounts=["acc-1"])
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    return captured[0]


def _prepared_plan(connection, statement, parameters, mode):
    names = list(dict.fromkeys(re.findall(r"%\((\w+)\)s", statement)))
    prepared = re.sub(
        r"%\((\w+)\)s", lambda match: f"${names.index(match[1]) + 1}", statement
    )
    connection.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute("PREPARE closeout_history_cost AS " + prepared)
        try:
            cursor.execute(
                pgsql.SQL(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE closeout_history_cost ({})"
                ).format(
                    pgsql.SQL(",").join(pgsql.Literal(parameters[n]) for n in names)
                )
            )
            return cursor.fetchone()[0][0]
        finally:
            cursor.execute("DEALLOCATE closeout_history_cost")


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("same_day_history", [0, 10000])
def test_history_lookup_does_not_read_other_dates(
    session, sync_seed, mode, present, same_day_history, record_property
):
    plan = plan_for(session, sync_seed)
    seed_yesterday(session, sync_seed)
    statement, parameters = _capture_history(session, plan)
    connection = session.connection()
    # 克隆迁移后的完整表/索引；大规模数据不污染共享表统计。
    connection.execute(
        text(
            "CREATE TEMP TABLE report_sync_job "
            "(LIKE public.report_sync_job INCLUDING ALL) ON COMMIT DROP"
        )
    )
    connection.execute(
        text("""
        INSERT INTO report_sync_job
          (id,tenant_id,bc_id,kind,priority,status,advertiser_ids,campaign_ids,
           start_date,end_date,job_key,frozen_route,actor_id,due_at,attempts,
           request_count,progress,created_at)
        SELECT gen_random_uuid(),:tenant,:bc,'ad_day',CASE WHEN i%4=0 THEN 3 ELSE 0 END,
          (ARRAY['COMPLETE','FAILED','CANCELLED','QUEUED','RUNNING','WAITING'])[1+i%6],
          jsonb_build_array('history-'||i),'[]',CAST(:day AS date)-1-(i%300),
          CAST(:day AS date)-1-(i%300),'history-'||i,CAST(:route AS jsonb),:actor,
          now()-i*interval '1 hour',0,0,jsonb_build_object('padding',repeat(md5(i::text),16)),now()
        FROM generate_series(1,240000) i
    """),
        {**parameters, "day": parameters["first_day"]},
    )
    connection.execute(
        text("""
        INSERT INTO report_sync_job
          (id,tenant_id,bc_id,kind,priority,status,advertiser_ids,campaign_ids,
           start_date,end_date,job_key,frozen_route,actor_id,due_at,attempts,
           request_count,progress,created_at)
        SELECT gen_random_uuid(),:tenant,:bc,kind,0,'COMPLETE',
          '["acc-1"]','[]',:day,:day,kind,CAST(:route AS jsonb),:actor,now(),0,0,'{}',now()
        FROM (VALUES ('ad_day'),('material_day')) k(kind)
    """),
        {**parameters, "day": parameters["first_day"]},
    )
    # 大BC同日仍可有上万条其他账户任务；日期索引不承诺账户JSON单点命中。
    connection.execute(
        text("""
        INSERT INTO report_sync_job
          (id,tenant_id,bc_id,kind,priority,status,advertiser_ids,campaign_ids,
           start_date,end_date,job_key,frozen_route,actor_id,due_at,attempts,
           request_count,progress,created_at)
        SELECT gen_random_uuid(),:tenant,:bc,'ad_day',0,'COMPLETE',
          jsonb_build_array('same-day-'||i),'[]',:day,:day,'same-day-'||i,
          CAST(:route AS jsonb),:actor,now(),0,0,'{}',now()
        FROM generate_series(1,:count) i
        """),
        {**parameters, "day": parameters["first_day"], "count": same_day_history},
    )
    connection.execute(text("ANALYZE report_sync_job"))
    parameters = {**parameters, "accounts": ["acc-1" if present else "absent"]}
    explanation = _prepared_plan(connection, statement, parameters, mode)
    record_property("actual_sql", statement)
    record_property("plan", json.dumps(explanation))
    assert explanation["Plan"]["Actual Rows"] == (2 if present else 0)
    scans = [
        n
        for n in _nodes(explanation["Plan"])
        if n.get("Relation Name") == "report_sync_job"
    ]
    assert scans
    # 检查实际物理工作量，不把本机毫秒数当线上3秒保证。
    assert (
        sum(n.get("Rows Removed by Filter", 0) for n in scans) <= same_day_history + 2
    )
    # 新执行围栏/成本字段会增加每行宽度，页预算按真实物理密度计算；
    # 仍限定到当天候选的比例，不能以全历史表变大放宽到全表扫描。
    pages = connection.execute(
        text(
            "SELECT pg_relation_size('pg_temp.report_sync_job') / current_setting('block_size')::int"
        )
    ).scalar_one()
    candidate_pages = ceil(pages * (same_day_history + 2) / (240002 + same_day_history))
    assert (
        sum(n.get("Local Hit Blocks", 0) + n.get("Local Read Blocks", 0) for n in scans)
        < 100 + 3 * candidate_pages
    )


@pytest.mark.parametrize(
    "status", ["QUEUED", "RUNNING", "WAITING", "COMPLETE", "FAILED", "CANCELLED"]
)
@pytest.mark.parametrize(
    "mismatch", [None, "tenant", "bc", "actor", "route", "day", "priority", "account"]
)
def test_history_dedup_preserves_all_statuses_and_exact_scope(
    session, sync_seed, status, mismatch
):
    plan = plan_for(session, sync_seed)
    day, _midnight = seed_yesterday(session, sync_seed)
    for kind in ("ad_day", "material_day"):
        session.add(
            ReportSyncJob(
                tenant_id=uuid4() if mismatch == "tenant" else plan.tenant_id,
                bc_id="other-bc" if mismatch == "bc" else plan.bc_id,
                actor_id=uuid4() if mismatch == "actor" else plan.actor_id,
                frozen_route={"other": True}
                if mismatch == "route"
                else plan.frozen_route,
                kind=kind,
                status=status,
                priority=1 if mismatch == "priority" else 0,
                advertiser_ids=["other"]
                if mismatch == "account"
                else ["acc-1", "other"],
                start_date=day - timedelta(days=int(mismatch == "day")),
                end_date=day - timedelta(days=int(mismatch == "day")),
                due_at=NOW,
                job_key=str(uuid4()),
            )
        )
    session.flush()
    assert planner.plan_day_closeout(session, plan, now=NOW, accounts=["acc-1"]) == (
        2 if mismatch else 0
    )
