"""活动目录索引在0045历史schema上升降，保留运行和发布事实。"""

from time import monotonic
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun
from tests.migration_database import historical_database
from tests.modules.ads.test_directory_requests import _route
from tests.modules.reporting.sync_conftest import sync_seed

REVISION = "0062_directory_pending_index"
PREVIOUS = "0045_directory_type_index"
INDEX = "ix_ad_directory_pending_account"


def test_normal_index_build_requires_stopped_writers(monkeypatch):
    # 在独立0045库持有普通建索引事务，真实验证发布必须停写的锁合同。
    with historical_database(monkeypatch, PREVIOUS) as (engine, _config):
        with engine.connect() as builder, engine.connect() as writer:
            transaction = builder.begin()
            try:
                builder.execute(
                    text(
                        f"CREATE INDEX {INDEX} ON ad_directory_run "
                        "(tenant_id,advertiser_id) WHERE status IN "
                        "('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')"
                    )
                )
                assert (
                    "ShareLock"
                    in builder.execute(
                        text("""
                    SELECT mode FROM pg_locks
                    WHERE pid=pg_backend_pid()
                      AND relation='ad_directory_run'::regclass AND granted
                    """)
                    )
                    .scalars()
                    .all()
                )
                writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                # 已持有建索引锁时读取仍可执行；即便零行UPDATE也须等写锁。
                assert (
                    writer.execute(
                        text("SELECT id FROM ad_directory_run LIMIT 1")
                    ).first()
                    is None
                )
                with pytest.raises(DBAPIError) as error:
                    writer.execute(
                        text("UPDATE ad_directory_run SET error_code=NULL WHERE false")
                    )
                assert getattr(error.value.orig, "sqlstate", None) == "55P03"
            finally:
                writer.rollback()
                transaction.rollback()


def test_pending_account_index_roundtrip_preserves_runs(monkeypatch, record_property):
    with historical_database(monkeypatch, PREVIOUS) as (engine, config):
        with Session(engine) as session, session.begin():
            seed = cast(Any, sync_seed).__wrapped__(session)
            run = AdDirectoryRun(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                bc_id="bc-a",
                actor_id=seed.actor_id,
                connection_id=seed.connection.id,
                frozen_route=_route(seed),
                partition_key="a" * 64,
                query={"include_deleted": True},
                status="QUEUED",
                claim_generation=1,
                kind="campaign",
                ad_type="SMART_PLUS",
            )
            # 0045只写当时真实列；当前ORM新增owner/发布合同字段不能渗入历史。
            table = Table(
                "ad_directory_run", MetaData(), autoload_with=session.connection()
            )
            session.execute(
                table.insert().values(
                    **{
                        key: value
                        for key, value in run.model_dump().items()
                        if key in table.c and key != "request_sequence"
                    }
                )
            )
            session.connection().execute(
                text("""
                INSERT INTO ad_directory_run OVERRIDING USER VALUE
                SELECT (jsonb_populate_record(NULL::ad_directory_run,
                  to_jsonb(r)||jsonb_build_object('id',gen_random_uuid(),
                    'request_id',gen_random_uuid(),
                    'status',CASE WHEN i<=5000 THEN 'QUEUED' ELSE 'COMPLETE' END,
                    'published_version',CASE WHEN i>5000 THEN i END))).*
                FROM ad_directory_run r CROSS JOIN generate_series(1,50000) i
                WHERE r.id=:id
                """),
                {"id": run.id},
            )
        with engine.connect() as connection:
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
            definition = connection.execute(
                text("SELECT pg_get_indexdef(to_regclass(:name))"), {"name": INDEX}
            ).scalar_one()
            assert "(tenant_id, advertiser_id)" in definition
            assert all(
                status in definition
                for status in ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")
            )
            record_property(
                "index_bytes",
                connection.execute(
                    text("SELECT pg_relation_size(to_regclass(:name))"), {"name": INDEX}
                ).scalar_one(),
            )
            before = connection.execute(
                text("SELECT count(*),sum(published_version) FROM ad_directory_run")
            ).one()
        command.downgrade(config, PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regclass(:name)"), {"name": INDEX}
                ).scalar_one()
                is None
            )
            assert (
                connection.execute(
                    text("SELECT count(*),sum(published_version) FROM ad_directory_run")
                ).one()
                == before
            )
        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regclass(:name)"), {"name": INDEX}
                ).scalar_one()
                is not None
            )
            assert (
                connection.execute(
                    text("SELECT count(*),sum(published_version) FROM ad_directory_run")
                ).one()
                == before
            )
