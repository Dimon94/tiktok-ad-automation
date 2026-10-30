"""同步查询索引往返、分区继承及真实扫描规模，不改变业务事实。"""

import json
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from uuid import uuid4

from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.partitions import ensure_month_partitions_for_range
from tests.migration_database import historical_database
from tests.modules.ads.test_directory_requests import _route
from tests.modules.reporting.sync_conftest import sync_seed

REVISION = "0044_sync_probe_indexes"
PREVIOUS = "0043_material_video_index"
INDEXES = {
    "ad_directory_run": "ix_ad_directory_recent_success",
    "ad_report_day": "ix_ad_report_day_spending",
    "material_report_day": "ix_material_report_day_spending",
}


def _scanned(node, table):
    rows = 0
    if node.get("Relation Name", "").startswith(table):
        rows = (
            node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
        ) * node.get("Actual Loops", 1)
    return rows + sum(_scanned(child, table) for child in node.get("Plans", []))


def _indexes(node):
    return [node.get("Index Name", "")] + [
        name for child in node.get("Plans", []) for name in _indexes(child)
    ]


def test_sync_probe_indexes_roundtrip_and_reduce_scans(monkeypatch, record_property):
    with historical_database(monkeypatch, PREVIOUS) as (engine, config):
        now = datetime(2026, 10, 12, 20, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session)
            ensure_month_partitions_for_range(
                session, date(2026, 10, 1), date(2026, 10, 1)
            )
            for table, identity, extra_columns, extra_values in (
                ("ad_report_day", "ad_id", ",campaign_id,adgroup_id", ",'c','g'"),
                (
                    "material_report_day",
                    "main_material_id",
                    ",main_material_type",
                    ",'VIDEO'",
                ),
            ):
                # 事实时区为+14，账户当前UTC；不得改用账户时区丢掉正消耗的明日事实。
                session.execute(
                    text(f"""
                    INSERT INTO {table} (tenant_id,advertiser_id,stat_date,{identity},
                      currency,timezone,fetched_at,sync_job_id,spend{extra_columns})
                    SELECT :tenant,'acc-2','2026-10-13'::date,'fact-'||i,
                      'USD','Pacific/Kiritimati',:now,:job,CASE WHEN i=0 THEN 1 ELSE 0 END{extra_values}
                    FROM generate_series(0,20000) i
                """),
                    {"tenant": seed.tenant_id, "now": now, "job": uuid4()},
                )
            runs = [
                AdDirectoryRun(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-2",
                    bc_id="bc-a",
                    actor_id=seed.actor_id,
                    connection_id=seed.connection.id,
                    channel="OFFICIAL_API",
                    frozen_route=_route(seed),
                    partition_key="a" * 64,
                    query={"include_deleted": True},
                    status="COMPLETE",
                    completed_at=now - timedelta(hours=1 if index == 0 else 48),
                    kind="campaign",
                    ad_type="SMART_PLUS",
                )
                for index in range(1001)
            ]
            # 明确验证0043历史结构，不能把最新ORM的owner/publish等新列写入旧表。
            historical = Table(
                "ad_directory_run", MetaData(), autoload_with=session.connection()
            )
            session.execute(
                historical.insert(),
                [
                    {
                        key: value
                        for key, value in run.model_dump().items()
                        if key in historical.c and key != "request_sequence"
                    }
                    for run in runs
                ],
            )
            session.flush()
            for table in INDEXES:
                session.execute(text(f"ANALYZE {table}"))
        params = {
            "tenant": seed.tenant_id,
            "ids": ["acc-1", "acc-2"],
            "now": now,
            "first": now.date() - timedelta(days=1),
            "last": now.date() + timedelta(days=1),
            "cutoff": now - timedelta(hours=24),
        }
        queries = {
            "ad_directory_run": """
                SELECT a.advertiser_id FROM unnest(CAST(:ids AS text[])) a(advertiser_id)
                JOIN LATERAL (SELECT 1 FROM ad_directory_run r
                  WHERE r.tenant_id=:tenant AND r.advertiser_id=a.advertiser_id
                    AND r.status='COMPLETE' AND CAST(r.query->>'include_deleted' AS boolean) IS true
                    AND r.completed_at>:cutoff LIMIT 1) recent ON true
            """,
        }
        for table, projection in (
            ("ad_report_day", "ad_id,adgroup_id,campaign_id"),
            ("material_report_day", "main_material_id"),
        ):
            queries[table] = f"""SELECT r.advertiser_id,{projection} FROM {table} r
                WHERE r.tenant_id=:tenant AND r.advertiser_id=ANY(:ids) AND r.spend>0
                  AND r.stat_date BETWEEN :first AND :last
                  AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date"""

        def measure():
            results = {}
            with engine.connect() as conn:
                for table, sql in queries.items():
                    rows = conn.execute(text(sql), params).all()
                    plan = conn.execute(
                        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params
                    ).scalar_one()[0]
                    results[table] = {
                        "rows": [list(row) for row in rows],
                        "scanned": _scanned(plan["Plan"], table),
                        "milliseconds": plan["Execution Time"],
                        "indexes": _indexes(plan["Plan"]),
                    }
            return results

        before = measure()
        started = monotonic()
        command.upgrade(config, REVISION)
        record_property("build_seconds", monotonic() - started)
        after = measure()
        for table in queries:
            assert after[table]["rows"] == before[table]["rows"]
            assert len(after[table]["rows"]) == 1
            assert before[table]["scanned"] > 100
            assert after[table]["scanned"] <= 2, after
            with engine.connect() as conn:
                family = set(
                    conn.execute(
                        text("""
                    SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
                    WHERE i.inhparent=CAST(:parent AS regclass)
                    UNION ALL SELECT CAST(:parent AS text)
                """),
                        {"parent": INDEXES[table]},
                    ).scalars()
                )
            assert family.intersection(after[table]["indexes"]), after
        # 既有DEFAULT/月分区与未来月分区都继承父索引，不在谓词里固定月份。
        with Session(engine) as session, session.begin():
            ensure_month_partitions_for_range(
                session, date(2026, 11, 1), date(2026, 11, 1)
            )
            for parent in (
                "ix_ad_report_day_spending",
                "ix_material_report_day_spending",
            ):
                assert (
                    session.execute(
                        text("""
                    SELECT count(*) FROM pg_inherits i JOIN pg_index x ON x.indexrelid=i.inhrelid
                    WHERE i.inhparent=CAST(:parent AS regclass) AND x.indisvalid
                """),
                        {"parent": parent},
                    ).scalar_one()
                    == 3
                )
        command.downgrade(config, PREVIOUS)
        with engine.connect() as conn:
            for name in INDEXES.values():
                assert (
                    conn.execute(
                        text("SELECT to_regclass(:name)"), {"name": name}
                    ).scalar_one()
                    is None
                )
        command.upgrade(config, REVISION)
        # 0044 往返验证结束后，按最新 head 与模型核对后续新增索引。
        command.upgrade(config, "head")
        command.check(config)
        repeated = measure()
        assert {k: v["rows"] for k, v in repeated.items()} == {
            k: v["rows"] for k, v in after.items()
        }
        record_property(
            "query_metrics",
            json.dumps({"before": before, "after": after, "reupgrade": repeated}),
        )
