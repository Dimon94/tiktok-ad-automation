"""目录类型索引必须保留任意层级/已删除对象语义，并缩短缺失类型的探测。"""

import json
from datetime import UTC, datetime
from time import monotonic

from alembic import command
from sqlalchemy import event, insert, text
from sqlmodel import Session

from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.directory_requests import directory_ad_types
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed
from tests.modules.reporting.test_sync_probe_indexes import _indexes, _scanned

PREVIOUS = "0044_sync_probe_indexes"
REVISION = "0045_directory_type_index"
INDEX = "ix_ad_object_type"


def test_directory_type_index_roundtrip_semantics_and_bounded_probes(
    monkeypatch, request
):
    with historical_database(monkeypatch, PREVIOUS) as (engine, config):
        now = datetime.now(UTC)
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            other = sync_seed.__wrapped__(session, historical=True)
            # 接近生产多账户分布，避免“全表只有一个重账户”使 LIMIT 成本偏向顺扫。
            session.execute(
                insert(AdvertiserAccount),
                [
                    {
                        "tenant_id": other.tenant_id,
                        "advertiser_id": f"background-{i}",
                        "timezone": "UTC",
                    }
                    for i in range(1000)
                ],
            )
            session.execute(
                text("""
                INSERT INTO ad_object (tenant_id,advertiser_id,kind,remote_id,ad_type,
                  name,configuration,observed_at,published_version)
                SELECT :tenant,'background-'||i,'ad','background-'||j,'REGULAR',
                  '',jsonb_build_object('payload',repeat('x',1000)),:now,1
                FROM generate_series(0,999) i CROSS JOIN generate_series(1,10) j
                """),
                {"tenant": other.tenant_id, "now": now},
            )
            session.execute(
                text("""
                INSERT INTO ad_object (tenant_id,advertiser_id,kind,remote_id,ad_type,
                  name,configuration,operation_status,observed_at,published_version)
                SELECT :tenant,'acc-1','ad','bulk-'||i,'SMART_PLUS',
                  '',jsonb_build_object('payload',repeat('x',1000)),'ENABLE',:now,1
                FROM generate_series(1,20000) i
                """),
                {"tenant": seed.tenant_id, "now": now},
            )
            # 创意和已删除广告都算使用过；不存在父系列不能改变判定。
            for tenant, account, kind, ad_type, status in (
                (seed.tenant_id, "acc-1", "creative", "LEGACY_SMART_PLUS", "DELETE"),
                (seed.tenant_id, "acc-2", "ad", "REGULAR", "DELETE"),
                (other.tenant_id, "acc-2", "campaign", "SMART_PLUS", "ENABLE"),
            ):
                session.execute(
                    text("""
                    INSERT INTO ad_object (tenant_id,advertiser_id,kind,remote_id,
                      ad_type,name,configuration,operation_status,observed_at,published_version)
                    VALUES (:tenant,:account,:kind,'rare',:type,'','{}',:status,:now,1)
                    """),
                    {
                        "tenant": tenant,
                        "account": account,
                        "kind": kind,
                        "type": ad_type,
                        "status": status,
                        "now": now,
                    },
                )
            session.execute(text("ANALYZE ad_object"))

        def measure():
            with Session(engine) as session:
                conn = session.connection()
                queries = []

                def capture(_conn, _cursor, sql, params, _context, _many):
                    if "FROM ad_object o" in sql:
                        queries.append((sql, params))

                event.listen(conn, "before_cursor_execute", capture)
                try:
                    choices = directory_ad_types(
                        session,
                        tenant_id=seed.tenant_id,
                        advertiser_ids=["acc-1", "acc-2"],
                        active_ids=[],
                    )
                finally:
                    event.remove(conn, "before_cursor_execute", capture)
                assert len(queries) == 1
                sql, params = queries[0]
                rows = sorted([list(row) for row in conn.exec_driver_sql(sql, params)])
                plan = conn.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params
                ).scalar_one()[0]
            return {
                "rows": rows,
                "choices": choices,
                "scanned": _scanned(plan["Plan"], "ad_object"),
                "milliseconds": plan["Execution Time"],
                "indexes": _indexes(plan["Plan"]),
            }

        before = measure()
        started = monotonic()
        # 只往返本索引迁移；不能先升级到不可逆创意合同再尝试降回0044。
        command.upgrade(config, REVISION)
        build_seconds = monotonic() - started
        after = measure()
        assert (
            after["rows"]
            == before["rows"]
            == [
                ["acc-1", "LEGACY_SMART_PLUS"],
                ["acc-1", "SMART_PLUS"],
                ["acc-2", "REGULAR"],
            ]
        )
        assert before["scanned"] >= 20000, before
        # 每账户一次任意对象探测 + 三种已知类型，各自首条即停。
        assert after["scanned"] <= 8, after
        assert INDEX in after["indexes"], after
        assert after["choices"] == before["choices"]
        with engine.connect() as conn:
            definition = (
                conn.execute(
                    text("""
                SELECT pg_get_indexdef(indexrelid) definition,
                  pg_relation_size(indexrelid) bytes,indisvalid,indisready
                FROM pg_index WHERE indexrelid=CAST(:name AS regclass)
                """),
                    {"name": INDEX},
                )
                .mappings()
                .one()
            )
            assert definition["indisvalid"] and definition["indisready"]
            assert "(tenant_id, advertiser_id, ad_type)" in definition["definition"]
            assert " WHERE " not in definition["definition"]
            index_bytes = definition["bytes"]
        command.downgrade(config, PREVIOUS)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT to_regclass(:name)"), {"name": INDEX}
                ).scalar_one()
                is None
            )
            assert (
                conn.execute(text("SELECT count(*) FROM ad_object")).scalar_one()
                == 30003
            )
        command.upgrade(config, REVISION)
        assert measure()["rows"] == after["rows"]
        command.upgrade(config, "head")
        command.check(config)
        request.node.user_properties.append(
            (
                "directory_type_index_metrics",
                json.dumps(
                    {
                        "build_seconds": build_seconds,
                        "index_bytes": index_bytes,
                        "before": before,
                        "after": after,
                    }
                ),
            )
        )
