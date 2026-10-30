#!/usr/bin/env python3
"""仅本机隔离perf库：测真实派生维护任务的排队、服务率、WAL和表大小。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tests.performance.sync_query_fixture import (
    validate_database_url as _validate_perf_url,
)


def validate_database_url(url: str) -> None:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        parsed = make_url(url)
    except ArgumentError:
        raise ValueError(
            "benchmark requires the dedicated local perf database"
        ) from None
    # 单独的小样本库与全量perf库都必须满足同一本机/端口/无query参数限制。
    if parsed.database == "sync_redesign_test_projection":
        url = parsed.set(database="sync_redesign_test_perf").render_as_string()
    _validate_perf_url(url)


def guard() -> None:
    validate_database_url(os.environ.get("DATABASE_URL", ""))
    if os.environ.get("APP_ENV_FILE") != "/dev/null":
        raise ValueError("APP_ENV_FILE must explicitly be /dev/null")
    if shutil.disk_usage(ROOT).free < 10 * 1024**3:
        raise ValueError("less than mandatory 10 GiB disk reserve")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ticks", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--enqueue-kind", choices=["ad_day", "material_day", "both"])
    args = parser.parse_args()
    guard()
    if not 1 <= args.ticks <= 120 or not 0 <= args.interval_seconds <= 30:
        raise ValueError("ticks must be 1..120 and interval 0..30 seconds")
    if args.enqueue_kind and not args.manifest:
        raise ValueError("explicit fixture manifest is required to mark buckets dirty")

    from app.core.db import engine
    from app.modules.reporting.read_models.tasks import project_read_models
    from sqlalchemy import event, text
    from sqlmodel import Session

    # .run()保留真实维护算法，但没有Celery进程的30/40秒软硬时限；独立容量
    # 工具给每条SQL设30秒上限，避免坏计划无限占用本机压力测试窗口。
    @event.listens_for(engine, "connect")
    def local_sql_deadline(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '30s'")
        connection.commit()

    def queue(session):
        return [
            dict(row)
            for row in session.execute(
                text("""
            SELECT kind,count(*) AS pending_buckets,
                max(extract(epoch FROM clock_timestamp()-observed_at)) AS oldest_seconds
            FROM read_bucket_state WHERE kind IN ('ad_day','material_day')
                AND source_revision>derived_revision GROUP BY kind ORDER BY kind
        """)
            ).mappings()
        ]

    result = {
        "database": "dedicated-local-perf",
        "slo_qualified": False,
        "worker": "reporting.project_read_models",
        "sql_statement_timeout_seconds": 30,
        "complete": False,
        "interval_seconds": args.interval_seconds,
        "note": "真实任务函数；无Celery投递延迟。所有本机perf库pending桶参与服务率；无外部API调用。",
        "ticks": [],
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str))

    with Session(engine) as session:
        result["postgresql"] = dict(
            session.execute(
                text("""
            SELECT version() AS version,current_setting('shared_buffers') AS shared_buffers,
                current_setting('work_mem') AS work_mem,
                (SELECT version_num FROM alembic_version) AS schema_revision
        """)
            )
            .mappings()
            .one()
        )
        if args.enqueue_kind:
            manifest = json.loads(args.manifest.read_text())
            kinds = (
                ["ad_day", "material_day"]
                if args.enqueue_kind == "both"
                else [args.enqueue_kind]
            )
            # 只改显式fixture的派生修订，不生成源事实，不改其他租户的待重建输入。
            tenant = manifest["tenant_id"]
            enqueued = session.execute(
                text("""
                UPDATE read_bucket_state SET source_revision=source_revision+1,
                    observed_at=clock_timestamp()
                WHERE tenant_id=CAST(:tenant AS uuid) AND kind=ANY(:kinds)
                RETURNING kind,bucket
            """),
                {"tenant": tenant, "kinds": kinds},
            ).all()
            result["enqueued_buckets"] = len(enqueued)
            session.commit()
        result["before"] = queue(session)
        result["queue_plan"] = session.execute(
            text("""
            EXPLAIN (ANALYZE,BUFFERS,SETTINGS,FORMAT JSON)
            SELECT tenant_id,advertiser_id,kind,bucket,source_revision FROM read_bucket_state
            WHERE source_revision>derived_revision AND kind IN ('ad_day','material_day')
            ORDER BY observed_at,tenant_id,advertiser_id,kind,bucket
            LIMIT 1 FOR UPDATE SKIP LOCKED
        """)
        ).scalar_one()
        # 释放EXPLAIN取得的行锁后，再让真实任务读取。
        session.rollback()
        lsn = session.execute(text("SELECT pg_current_wal_insert_lsn()")).scalar_one()
    started = time.perf_counter()
    save()
    try:
        for index in range(args.ticks):
            tick_started = time.perf_counter()
            completed = project_read_models.run()
            elapsed = time.perf_counter() - tick_started
            with Session(engine) as session:
                pending = queue(session)
            result["ticks"].append(
                {
                    "index": index,
                    "completed_buckets": completed,
                    "service_seconds": elapsed,
                    "pending": pending,
                }
            )
            save()
            if not pending:
                break
            if index + 1 < args.ticks:
                time.sleep(max(0, args.interval_seconds - elapsed))
        result["complete"] = True
    finally:
        elapsed = time.perf_counter() - started
        result["elapsed_seconds"] = elapsed
        completed = sum(tick["completed_buckets"] for tick in result["ticks"])
        result["completed_buckets"] = completed
        result["wall_buckets_per_second"] = completed / elapsed if elapsed else None
        service = sum(tick["service_seconds"] for tick in result["ticks"])
        result["active_buckets_per_second"] = completed / service if service else None
        result["scheduled_buckets_per_second"] = (
            completed
            / sum(
                max(args.interval_seconds, tick["service_seconds"])
                for tick in result["ticks"]
            )
            if result["ticks"]
            else None
        )
        with Session(engine) as session:
            result["after"] = queue(session)
            result["wal_bytes"] = session.execute(
                text(
                    "SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),CAST(:lsn AS pg_lsn))"
                ),
                {"lsn": lsn},
            ).scalar_one()
            result["relations"] = [
                dict(row)
                for row in session.execute(
                    text("""
                SELECT c.relname,pg_relation_size(c.oid) AS heap_bytes,
                    pg_indexes_size(c.oid) AS index_bytes,
                    CASE WHEN c.reltoastrelid=0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname IN ('read_bucket_state',
                    'adgroup_report_day','material_entity_day','ad_entity_read',
                    'campaign_name_current','current_ad_material') ORDER BY c.relname
            """)
                ).mappings()
            ]
        save()


if __name__ == "__main__":
    main()
