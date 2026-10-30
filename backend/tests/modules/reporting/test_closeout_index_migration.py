"""收尾历史索引在独立0062库升降，不改任务与执行计数。"""

from time import monotonic
from typing import Any, cast

from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlmodel import Session

from app.modules.reporting.store_models import ReportSyncJob
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed
from tests.modules.reporting.test_day_closeout import NOW, plan_for

REVISION = "0064_closeout_job_scope"
PREVIOUS = "0062_directory_pending_index"
INDEX = "ix_report_closeout_scope"


def _facts(connection):
    return connection.execute(
        text("""
        SELECT status,count(*),sum(request_count),sum(attempts)
        FROM report_sync_job GROUP BY status ORDER BY status
        """)
    ).all()


def test_closeout_index_roundtrip_preserves_all_job_states(
    monkeypatch, record_property
):
    with historical_database(monkeypatch, PREVIOUS) as (engine, config):
        with Session(engine) as session, session.begin():
            seed = cast(Any, sync_seed).__wrapped__(session)
            plan = plan_for(session, seed)
            job = ReportSyncJob(
                tenant_id=seed.tenant_id,
                bc_id=plan.bc_id,
                actor_id=seed.actor_id,
                frozen_route=plan.frozen_route,
                kind="ad_day",
                priority=0,
                status="QUEUED",
                advertiser_ids=["acc-1"],
                start_date=NOW.date(),
                end_date=NOW.date(),
                job_key="closeout-migration-seed",
                due_at=NOW,
            )
            # 历史DDL测试必须按当时表列写入，不能用当前ORM新增字段。
            historical = Table(
                "report_sync_job", MetaData(), autoload_with=session.connection()
            )
            session.execute(
                historical.insert().values(
                    **{
                        key: value
                        for key, value in job.model_dump().items()
                        if key in historical.c
                    }
                )
            )
            session.connection().execute(
                text("""
                INSERT INTO report_sync_job
                SELECT (jsonb_populate_record(NULL::report_sync_job,
                  to_jsonb(j)||jsonb_build_object(
                    'id',gen_random_uuid(),'job_key','migration-'||i,
                    'priority',CASE WHEN i%4=0 THEN 3 ELSE 0 END,
                    'status',(ARRAY['QUEUED','RUNNING','WAITING',
                      'COMPLETE','FAILED','CANCELLED'])[1+i%6],
                    'request_count',i,'attempts',i%7))).*
                FROM report_sync_job j CROSS JOIN generate_series(1,50000) i
                WHERE j.id=:id
                """),
                {"id": job.id},
            )
        with engine.connect() as connection:
            before = _facts(connection)
            assert sum(row[1] for row in before) == 50001
            assert (
                connection.execute(
                    text("SELECT to_regclass(:name)"), {"name": INDEX}
                ).scalar_one()
                is None
            )

        started = monotonic()
        command.upgrade(config, REVISION)
        record_property("upgrade_50001_rows_seconds", monotonic() - started)
        with engine.connect() as connection:
            row = connection.execute(
                text("""
                SELECT indisvalid,indisready,pg_get_indexdef(indexrelid) AS definition,
                  pg_get_expr(indpred,indrelid) AS predicate
                FROM pg_index WHERE indexrelid=to_regclass(:name)
                """),
                {"name": INDEX},
            ).one()
            assert row.indisvalid and row.indisready
            assert "(tenant_id, bc_id, actor_id, end_date)" in row.definition
            assert "priority = 0" in row.predicate
            assert "start_date = end_date" in row.predicate
            assert "status" not in row.predicate
            assert _facts(connection) == before
            record_property(
                "index_bytes",
                connection.execute(
                    text("SELECT pg_relation_size(to_regclass(:name))"), {"name": INDEX}
                ).scalar_one(),
            )

        command.downgrade(config, PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regclass(:name)"), {"name": INDEX}
                ).scalar_one()
                is None
            )
            assert _facts(connection) == before
        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regclass(:name)"), {"name": INDEX}
                ).scalar_one()
                is not None
            )
            assert _facts(connection) == before
