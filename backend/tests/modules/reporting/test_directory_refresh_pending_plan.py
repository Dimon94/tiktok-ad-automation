"""真实通用计划仍使用在途部分索引，终态历史不进入热报目录探测。"""

import re
from datetime import UTC, datetime
from inspect import unwrap
from typing import Any
from uuid import uuid4

import pytest
from psycopg import ClientCursor
from psycopg import sql as driver_sql
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync import _queue_missing_parents
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import refresh_report_directory
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture
from tests.modules.reporting.test_directory_refresh import prepared
from tests.modules.reporting.test_jobs import _route
from tests.modules.reporting.test_sync_probe_indexes import _indexes, _scanned


def _run(seed, **changes):
    fields: dict[str, Any] = {
        "tenant_id": seed.tenant_id,
        "advertiser_id": "acc-2",
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "connection_id": seed.connection.id,
        "frozen_route": _route(seed),
        "partition_key": "a" * 64,
        "query": {"include_deleted": True, "ids": ["missing-campaign"]},
        "status": "QUEUED",
        "kind": "campaign",
        "ad_type": "SMART_PLUS",
    }
    return AdDirectoryRun(**(fields | changes))


def _prepare_actual_pending_query(connection, sql, params):
    # 捕获生产入口真正发出的 SQL，只把 driver 占位符换成 PREPARE 参数。
    names = []

    def parameter(match):
        name = match.group(1)
        if name not in names:
            names.append(name)
        return f"${names.index(name) + 1}"

    prepared_sql = re.sub(r"%\(([^)]+)\)s", parameter, sql)
    name = "pending_plan_" + uuid4().hex
    connection.exec_driver_sql(f"PREPARE {name} AS {prepared_sql}")
    arguments = driver_sql.SQL(", ").join(driver_sql.Placeholder(key) for key in names)
    # EXECUTE 是 utility statement，使用驱动安全字面量适配；PREPARE 的计划仍强制 generic。
    with ClientCursor(connection.connection.driver_connection) as cursor:
        execute = cursor.mogrify(
            driver_sql.SQL("EXECUTE {}({})").format(
                driver_sql.Identifier(name), arguments
            ),
            {key: params[key] for key in names},
        )
    return name, execute


@pytest.mark.parametrize("history", [0, 20000])
@pytest.mark.parametrize("entry", ["report_refresh", "parent_followup"])
def test_generic_pending_plan_is_bounded_and_preserves_frozen_scope(
    monkeypatch, history, entry, record_property
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    # 每个参数独占临时测试库，迁移至代码真实head；不降级/清理共享性能库。
    with historical_database(monkeypatch, "head") as (engine, _config):
        with Session(engine, expire_on_commit=False) as session:
            sync_seed = unwrap(seed_fixture)(session)
            # 零消耗仍走相同 pending SELECT，但不会产生无关目录请求。
            job = prepared(session, sync_seed, spend="0")
            other = unwrap(seed_fixture)(session)
            active = [
                _run(sync_seed, status=status)
                for status in ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")
            ]
            excluded = [
                *[
                    _run(sync_seed, status=status)
                    for status in ("COMPLETE", "FAILED", "CANCELLED", "STALE")
                ],
                _run(
                    sync_seed,
                    bc_id="bc-b",
                    frozen_route=_route(sync_seed) | {"bc_id": "bc-b"},
                ),
                _run(sync_seed, advertiser_id="acc-1"),
                _run(sync_seed, actor_id=other.actor_id),
                _run(sync_seed, workload="background"),
                _run(
                    sync_seed,
                    frozen_route=_route(sync_seed) | {"binding_revision": 99},
                ),
                _run(other),
            ]
            different_type = _run(sync_seed, ad_type="REGULAR")
            excluded.append(different_type)
            session.add_all(active + excluded)
            session.flush()
            connection = session.connection()
            # 模拟其它账户仍有在途工作，避免只有几个全局活动行时 due 索引同样便宜。
            connection.execute(
                text("""
                INSERT INTO advertiser_account
                SELECT (jsonb_populate_record(NULL::advertiser_account,
                  to_jsonb(a)||jsonb_build_object('advertiser_id','noise-'||i))).*
                FROM advertiser_account a CROSS JOIN generate_series(0,99) i
                WHERE a.tenant_id=:tenant AND a.advertiser_id='acc-1'
                """),
                {"tenant": sync_seed.tenant_id},
            )
            connection.execute(
                text("""
                INSERT INTO ad_directory_run OVERRIDING USER VALUE
                SELECT (jsonb_populate_record(NULL::ad_directory_run,
                  to_jsonb(r)||jsonb_build_object('id',gen_random_uuid(),
                    'request_id',gen_random_uuid(),'advertiser_id','noise-'||(i%100)))).*
                FROM ad_directory_run r CROSS JOIN generate_series(1,5000) i
                WHERE r.id=:id
                """),
                {"id": active[0].id},
            )
            if history:
                connection.execute(
                    text("""
                    INSERT INTO ad_directory_run OVERRIDING USER VALUE
                    SELECT (jsonb_populate_record(NULL::ad_directory_run,
                      to_jsonb(r)||jsonb_build_object('id',gen_random_uuid(),
                        'request_id',gen_random_uuid(),'status','COMPLETE'))).*
                    FROM ad_directory_run r CROSS JOIN generate_series(1,:history) i
                    WHERE r.id=:id
                    """),
                    {"id": active[0].id, "history": history},
                )
            # 历史必须对另一个事务可见后再采样统计。外层回滚夹具的ANALYZE可能
            # 被autovacuum按其不可见快照覆盖，不能代表已有5k在途/20k终态的执行计划。
            session.commit()
            with engine.connect() as visible:
                assert visible.execute(
                    text("SELECT count(*) FROM ad_directory_run")
                ).scalar_one() == 5000 + history + len(active + excluded)
            connection = session.connection()
            connection.execute(text("ANALYZE ad_directory_run"))
            session.commit()
            connection = session.connection()
            record_property(
                "fixture_committed_rows", 5000 + history + len(active + excluded)
            )
            statistics = (
                connection.execute(
                    text("""
                SELECT relname, relpages, reltuples FROM pg_class
                WHERE oid IN ('ad_directory_run'::regclass,
                              'ix_ad_directory_pending_account'::regclass)
                ORDER BY relname
            """)
                )
                .mappings()
                .all()
            )
            record_property("fixture_statistics", [dict(row) for row in statistics])
            captured = []

            def capture(_conn, _cursor, sql, params, _context, _many):
                if sql.startswith("SELECT ad_directory_run."):
                    captured.append((sql, params))

            event.listen(connection, "before_cursor_execute", capture)
            try:
                if entry == "report_refresh":
                    assert refresh_report_directory(session, job) == 0
                else:
                    _queue_missing_parents(
                        session,
                        run=active[0],
                        objects=[
                            AdObject(
                                tenant_id=sync_seed.tenant_id,
                                advertiser_id="acc-2",
                                kind="adgroup",
                                remote_id="child",
                                ad_type="SMART_PLUS",
                                parent_kind="campaign",
                                parent_remote_id="missing-campaign",
                                observed_at=datetime.now(UTC),
                                published_version=1,
                            )
                        ],
                    )
            finally:
                event.remove(connection, "before_cursor_execute", capture)
            assert len(captured) == 1
            connection.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
            name, execute = _prepare_actual_pending_query(connection, *captured[0])
            try:
                returned = connection.exec_driver_sql(execute).mappings().all()
                # 报表不筛广告类型，父补读在 SQL 后按原规则处理 workload；不收窄旧语义。
                expected = active + (
                    [different_type]
                    if entry == "report_refresh"
                    else [run for run in excluded if run.workload == "background"]
                )
                assert {row["id"] for row in returned} == {run.id for run in expected}
                explanation = connection.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + execute
                ).scalar_one()[0]
                plan = explanation["Plan"]
                record_property("plan", explanation)
                # 小表可以顺序扫描；有历史时必须走正式在途索引，不强迫优化器选索引。
                if history:
                    assert "ix_ad_directory_pending_account" in _indexes(plan)
                assert _scanned(plan, "ad_directory_run") <= len(active + excluded)
                assert (
                    plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0)
                    < 128
                )
                assert plan.get("Temp Written Blocks", 0) == 0
            finally:
                connection.exec_driver_sql(f"DEALLOCATE {name}")


def test_unknown_status_cannot_enter_pending_store(session, sync_seed):
    # 当前数据库已拒绝未知状态，不能为了测试查询而放松生产状态约束。
    with pytest.raises(IntegrityError, match="ck_ad_directory_run_status"):
        with session.begin_nested():
            session.add(_run(sync_seed, status="UNKNOWN"))
            session.flush()
