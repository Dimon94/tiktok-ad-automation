"""真实PG执行计划：大批次不能让后续单对象发布扫描整个投影表。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session

from app.core.db import engine
from app.modules.ads.sync import _publish_staged_entities, _stage_entities


def test_small_entity_publish_replans_after_large_transition_and_keeps_session(
    session, directory_seed
):
    seed = directory_seed
    raw = session.connection().connection.driver_connection
    plans = []

    def capture(diag):
        message = diag.message_primary or ""
        if "plan:" in message and "{" in message:
            plans.append(json.loads(message[message.index("{") :]))

    session.execute(text("LOAD 'auto_explain'"))
    for setting, value in {
        "client_min_messages": "log",
        "auto_explain.log_min_duration": "-1",
        "auto_explain.log_analyze": "on",
        "auto_explain.log_buffers": "on",
        "auto_explain.log_timing": "off",
        "auto_explain.log_nested_statements": "on",
        "auto_explain.log_format": "json",
    }.items():
        session.execute(text(f"SET LOCAL {setting}='{value}'"))
    raw.add_notice_handler(capture)
    try:
        session.execute(
            text("""
            INSERT INTO ad_object
              (tenant_id,advertiser_id,kind,remote_id,ad_type,name,configuration,
               observed_at,published_version,source_connection_id,source_channel)
            SELECT :tenant,:advertiser,'campaign','cached-plan-'||s,'REGULAR','bulk',
                '{}'::jsonb,now(),1,:connection,'OFFICIAL_API'
            FROM generate_series(1,20000) s
        """),
            {
                "tenant": seed.context.tenant_id,
                "advertiser": seed.ref.advertiser_id,
                "connection": seed.connection.id,
            },
        )
        session.execute(text("ANALYZE ad_entity_read"))
        run = SimpleNamespace(
            tenant_id=seed.context.tenant_id,
            advertiser_id=seed.ref.advertiser_id,
            connection_id=seed.connection.id,
            channel="OFFICIAL_API",
            ad_type="REGULAR",
        )
        staged = _stage_entities(
            session,
            run,
            2,
            datetime.now(UTC),
            [
                [
                    {
                        "ref": {
                            "tenant_id": str(run.tenant_id),
                            "advertiser_id": run.advertiser_id,
                            "kind": "campaign",
                            "remote_id": "cached-plan-1",
                        },
                        "ad_type": "REGULAR",
                        "name": "single refreshed object",
                        "configuration": {},
                    }
                ]
            ],
        )
        # 确定从大批次开始缓存trigger计划，避免连接池前一个测试影响复现。
        for _ in range(7):
            session.execute(text("DISCARD PLANS"))
        session.execute(
            text("""
            UPDATE ad_object SET name='warm-large'
            WHERE tenant_id=:tenant AND advertiser_id=:advertiser
                AND kind='campaign' AND remote_id LIKE 'cached-plan-%'
        """),
            {"tenant": run.tenant_id, "advertiser": run.advertiser_id},
        )
        lock_key = uuid4().int % (2**63 - 1)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        assert raw.execute("SELECT %s::int", (7,), prepare=True).fetchone()[0] == 7
        # 只采集待断言的单对象发布；两万行预热仍执行，但不输出逐行嵌套计划。
        session.execute(text("SET LOCAL auto_explain.log_min_duration=0"))
        plans.clear()
        published = [
            obj
            for batch in _publish_staged_entities(session, run, staged)
            for obj in batch
        ]
        assert len(published) == 1 and published[0].name == "single refreshed object"
        deletes = [
            p
            for p in plans
            if p.get("Query Text", "").lstrip().startswith("DELETE FROM ad_entity_read")
        ]
        assert deletes, "auto_explain must capture the real projection trigger"

        def scans(node):
            if (
                node.get("Relation Name") == "ad_entity_read"
                and "Scan" in node["Node Type"]
            ):
                yield node.get("Actual Rows", 0) * node.get("Actual Loops", 1)
            for child in node.get("Plans", []):
                yield from scans(child)

        touched = sum(scans(deletes[-1]["Plan"]))
        assert touched <= 10, f"single publication scanned {touched} projection rows"
        # 仅刷新计划，不丢临时页、事务锁或psycopg已命名的准备语句。
        assert session.execute(text(f"SELECT count(*) FROM {staged}")).scalar_one() == 1
        assert raw.execute("SELECT %s::int", (8,), prepare=True).fetchone()[0] == 8
        with Session(engine) as competitor:
            assert (
                competitor.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
                ).scalar_one()
                is False
            )
    finally:
        raw.remove_notice_handler(capture)
