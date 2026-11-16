"""真实调度 SQL 按租户/工作类找最老就绪头，不扫描排序所有到期头。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alembic import command
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.modules.reporting import job_tasks  # noqa: F401
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.models import SyncSchedule, SyncScope
from app.modules.sync_control.schedule import report_endpoint
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture
from tests.modules.sync_control.test_dispatch_interleavings import enqueue

INDEX = "ix_sync_schedule_service_age"
REVISION = "0118_sync_schedule_service_age"
PREVIOUS = "0117_directory_status_version"
CLASSES = ("realtime", "history", "directory", "maintenance")


def _scope(session, template):
    row = SyncScope(
        **dict(template.model_dump(), id=uuid4(), identity_digest=uuid4().hex * 2)
    )
    session.add(row)
    session.flush()
    return row.id


def _heads(session, template, now):
    # future/lease 头的 pending age 更早，但尚不能拿走 due 头的机会。
    cases = {
        "inactive": {
            "active": False,
            "last_dispatched_at": None,
            "oldest_pending_at": None,
        },
        "future": {
            "next_eligible_at": now + timedelta(hours=1),
            "last_dispatched_at": None,
            "oldest_pending_at": now - timedelta(days=30),
        },
        "leased": {
            "leased_until": now + timedelta(hours=1),
            "last_dispatched_at": None,
            "oldest_pending_at": now - timedelta(days=40),
        },
        "null-age": {"last_dispatched_at": None, "oldest_pending_at": None},
        "expired": {
            "leased_until": now,
            "last_dispatched_at": None,
            "oldest_pending_at": now - timedelta(days=20),
        },
        "pending-age": {
            "last_dispatched_at": None,
            "oldest_pending_at": now - timedelta(days=10),
        },
        "dispatched-age": {
            "last_dispatched_at": now - timedelta(days=100),
            "oldest_pending_at": None,
        },
        "other-endpoint": {
            "physical_endpoint": "/unrelated/",
            "last_dispatched_at": None,
            "oldest_pending_at": None,
        },
    }
    ids = {}
    for label, overrides in cases.items():
        identity = ids[label] = _scope(session, template)
        for work_class in CLASSES:
            session.add(
                SyncSchedule(
                    **{
                        "scope_id": identity,
                        "tenant_id": template.tenant_id,
                        "work_class": work_class,
                        "active": True,
                        "next_eligible_at": now,
                        "physical_endpoint": report_endpoint("ad_day"),
                    }
                    | overrides
                )
            )
    session.flush()
    return ids


def _noise(session, template, now):
    # 各租户四类各有4000个更年轻的到期头；旧计划每类都先扫完、再排序。
    session.execute(
        text("""
        WITH scopes AS (
          INSERT INTO sync_scope
          SELECT (jsonb_populate_record(NULL::sync_scope,
            to_jsonb(s) || jsonb_build_object('id',gen_random_uuid(),
              'identity_digest',md5(s.id::text||n::text)||md5(s.id::text||n::text)))).*
          FROM sync_scope s CROSS JOIN generate_series(1,4000) n WHERE s.id=:scope
          RETURNING id,tenant_id
        )
        INSERT INTO sync_schedule
          (scope_id,tenant_id,work_class,active,next_eligible_at,physical_endpoint,
           cost,deficit,generation,oldest_pending_at,last_dispatched_at)
        SELECT id,tenant_id,c,true,:now,:endpoint,1,0,0,:now,:now FROM scopes
        CROSS JOIN (VALUES ('realtime'),('history'),('directory'),('maintenance')) classes(c)
    """),
        {"scope": template.id, "now": now, "endpoint": report_endpoint("ad_day")},
    )


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def test_actual_ready_union_has_bounded_service_age_scan_and_same_eligibility(
    session, sync_seed, redis_client, record_property
):
    now = datetime.now(UTC)
    enqueue(session, sync_seed, now, tier="hot", accounts=["acc-1"])
    statements = []
    connection = session.connection()

    def capture(_connection, _cursor, sql, params, _context, _many):
        if (
            "UNION ALL" in sql
            and "sync_schedule.last_dispatched_at ASC NULLS FIRST" in sql
        ):
            statements.append((sql, deepcopy(params)))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert (
            dispatch_due(session, redis_client, now=now, physical_slots=8, max_claims=1)
            == 1
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements
    sql, parameters = statements[0]
    templates = [
        session.exec(
            select(SyncScope).where(SyncScope.tenant_id == sync_seed.tenant_id)
        ).one()
    ]
    other = seed_fixture.__wrapped__(session)
    enqueue(session, other, now, tier="hot", accounts=["acc-1"])
    templates.append(
        session.exec(
            select(SyncScope).where(SyncScope.tenant_id == other.tenant_id)
        ).one()
    )
    # 捕获实际SQL后，真实任务头退出本次只读重放候选，避免与精确年龄fixture平局。
    session.execute(text("UPDATE sync_schedule SET active=false"))
    expected = {}
    for template in templates:
        expected[template.tenant_id] = _heads(session, template, now)
        _noise(session, template, now)
    session.execute(text("ANALYZE sync_schedule"))
    visited = []
    for template in templates:
        params = {
            key: template.tenant_id if value == sync_seed.tenant_id else value
            for key, value in parameters.items()
        }
        ids = expected[template.tenant_id]
        # NULL last_dispatched 先于任何已派发时间；其内部 NULL pending 又最优先。
        for label in ("null-age", "expired", "pending-age", "dispatched-age"):
            rows = connection.exec_driver_sql(sql, params).mappings().all()
            assert {(row["scope_id"], row["work_class"]) for row in rows} == {
                (ids[label], c) for c in CLASSES
            }
            plan = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, params
            ).scalar_one()[0]
            nodes = list(_nodes(plan["Plan"]))
            assert not any(
                n["Node Type"] in ("Seq Scan", "Sort", "Incremental Sort")
                for n in nodes
            ), plan
            scans = [n for n in nodes if n.get("Relation Name") == "sync_schedule"]
            count = sum(
                (n["Actual Rows"] + n.get("Rows Removed by Filter", 0))
                * n["Actual Loops"]
                for n in scans
            )
            assert count <= 64, plan
            assert sum(n.get("Index Name") == INDEX for n in scans) == 4, plan
            visited.append(count)
            session.execute(
                text("UPDATE sync_schedule SET active=false WHERE scope_id=:scope"),
                {"scope": ids[label]},
            )
        # 推进参数时间后，租约到期/未来到期再次参与原有年龄排序。
        later = {
            key: now + timedelta(hours=2) if value == now else value
            for key, value in params.items()
        }
        rows = connection.exec_driver_sql(sql, later).mappings().all()
        assert {(row["scope_id"], row["work_class"]) for row in rows} == {
            (ids["leased"], c) for c in CLASSES
        }
    record_property("noise_heads", 32000)
    record_property("max_schedule_rows_visited", max(visited))


def test_service_age_index_upgrade_downgrade_upgrade_preserves_schedule(monkeypatch):
    with historical_database(monkeypatch, PREVIOUS) as (engine, config):
        with Session(engine) as session, session.begin():
            seed = seed_fixture.__wrapped__(session, historical=True)
            now = datetime.now(UTC)
            from app.modules.sync_control.scopes import ensure_scope
            from tests.modules.reporting.test_jobs import _route

            # 旧索引迁移只播种当时的 scope/head，不能调用今日要求新授权列的入队服务。
            template = ensure_scope(
                session,
                route=_route(seed),
                actor_id=seed.actor_id,
                advertiser_id="acc-1",
                resource="ad_day",
                query={"campaign_ids": ["scope"]},
                timezone="Asia/Hong_Kong",
                metric_contract="report-v2",
            )
            session.add(
                SyncSchedule(
                    scope_id=template.id,
                    tenant_id=seed.tenant_id,
                    work_class="realtime",
                    next_eligible_at=now,
                    oldest_pending_at=now,
                    physical_endpoint=report_endpoint("ad_day"),
                )
            )
            _heads(session, template, now)
        with engine.connect() as connection:
            before = (
                connection.execute(
                    text(
                        "SELECT to_jsonb(s) FROM sync_schedule s ORDER BY scope_id,work_class"
                    )
                )
                .scalars()
                .all()
            )
        for target in (REVISION, PREVIOUS, REVISION):
            (command.downgrade if target == PREVIOUS else command.upgrade)(
                config, target
            )
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT to_jsonb(s) FROM sync_schedule s ORDER BY scope_id,work_class"
                        )
                    )
                    .scalars()
                    .all()
                    == before
                )
                index = connection.execute(
                    text("""SELECT indisvalid,indisready,pg_get_indexdef(indexrelid)
                    FROM pg_index WHERE indexrelid=to_regclass(:name)"""),
                    {"name": INDEX},
                ).one_or_none()
                if target == PREVIOUS:
                    assert index is None
                else:
                    assert index[0] and index[1]
                    assert (
                        "(tenant_id, physical_endpoint, work_class, last_dispatched_at NULLS FIRST, oldest_pending_at NULLS FIRST)"
                        in index[2]
                    )
                    assert (
                        "INCLUDE (scope_id, next_eligible_at, leased_until)" in index[2]
                    )
                    assert "WHERE (active IS TRUE)" in index[2]
