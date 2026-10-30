#!/usr/bin/env python3
"""隔离本机 PG 的真实查询/发布基线；不会启动 API、Celery 或远端 provider。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import resource
import shutil
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tests.performance.sync_query_fixture import (
    FixtureParameters,
    budget_for_rows,
    expected_metrics,
    seed_fixture,
    validate_database_url,
)


def guard() -> None:
    validate_database_url(os.environ.get("DATABASE_URL", ""))
    if os.environ.get("APP_ENV_FILE") != "/dev/null":
        raise ValueError("APP_ENV_FILE must explicitly be /dev/null")
    if shutil.disk_usage(ROOT).free < 10 * 1024**3:
        raise ValueError("less than mandatory 10 GiB disk reserve")


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def prepare_old() -> Path:
    path = ROOT / ".runtime/sync-query/old-4656997e/backend"
    if not (path / "app/modules/reporting/queries.py").exists():
        raise ValueError("read-only old backend/app snapshot is missing")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["seed", "query", "publish", "budget", "rebuild"]
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objects", type=int, default=1000)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--active-fraction", type=float, default=0.1)
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--code", choices=["current", "old"], default="current")
    parser.add_argument(
        "--dimensions", default="account,campaign,adgroup,ad,material,drama"
    )
    parser.add_argument("--windows", default="1,7,31,90")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--include-empty", action="store_true")
    parser.add_argument("--search")
    parser.add_argument("--sort-by", default="spend")
    args = parser.parse_args()
    guard()
    if args.code == "old":
        sys.path.insert(0, str(prepare_old()))
    from app.core.db import engine
    from sqlalchemy import event, text
    from sqlmodel import Session

    result = {
        "action": args.action,
        "code": args.code,
        "baseline_revision": "4656997e" if args.code == "old" else "working-tree",
        "started_at": time.time(),
        "environment": {
            "cpu_count": os.cpu_count(),
            "free_disk_bytes": shutil.disk_usage(ROOT).free,
            "database": "dedicated-local-perf",
            "cache_state": "OS/PG cache not evicted; uncached query snapshots only",
            "slo_qualified": False,
            "full_scale": False,
        },
        "complete": False,
    }
    try:
        with Session(engine) as metadata_session:
            result["environment"]["postgresql"] = dict(
                metadata_session.execute(
                    text(
                        "SELECT version() AS version, current_setting('shared_buffers') AS shared_buffers, "
                        "current_setting('work_mem') AS work_mem, current_setting('max_connections') AS max_connections, "
                        "pg_database_size(current_database()) AS database_bytes"
                    )
                )
                .mappings()
                .one()
            )
            result["environment"]["schema_revision"] = metadata_session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        if args.action == "seed":
            if args.manifest.exists():
                raise ValueError(
                    "manifest already exists; reuse it or choose another file; no destructive reset"
                )
            parameters = FixtureParameters(
                objects=args.objects,
                days=args.days,
                active_fraction=args.active_fraction,
            )
            if args.objects > 1000:
                sample_path = ROOT / ".runtime/sync-query/sample-1000.json"
                if not sample_path.exists():
                    raise ValueError("measure the 1000-object sample before expanding")
                sample = json.loads(sample_path.read_text())
                budget = budget_for_rows(
                    rows=max(
                        args.objects,
                        int(
                            sample["parameters"]["objects"]
                            * parameters.active_ads
                            * parameters.days
                            / (
                                sample["parameters"]["active_ads"]
                                * sample["parameters"]["days"]
                            )
                        ),
                    ),
                    sample_rows=sample["parameters"]["objects"],
                    **sample["storage_delta"],
                    wal_bytes=sample["wal_bytes"],
                    free_bytes=shutil.disk_usage(ROOT).free,
                )
                result["budget"] = budget
                if not budget["allowed"]:
                    raise ValueError(
                        "measured capacity forecast would violate 10 GiB reserve"
                    )
            started = time.perf_counter()
            fixture = seed_fixture(engine, parameters)
            write(args.manifest, fixture)
            result.update(fixture=fixture, seed_seconds=time.perf_counter() - started)
        else:
            fixture = json.loads(args.manifest.read_text())
            raw = fixture["parameters"]
            parameters = FixtureParameters(
                **{
                    key: date.fromisoformat(value) if key == "end_date" else value
                    for key, value in raw.items()
                    if key in FixtureParameters.__dataclass_fields__
                }
            )
            if args.action == "budget":
                result["budget"] = budget_for_rows(
                    rows=max(
                        args.objects,
                        int(
                            sample["parameters"]["objects"]
                            * parameters.active_ads
                            * parameters.days
                            / (
                                sample["parameters"]["active_ads"]
                                * sample["parameters"]["days"]
                            )
                        ),
                    ),
                    sample_rows=parameters.objects,
                    **fixture["storage_delta"],
                    wal_bytes=fixture["wal_bytes"],
                    free_bytes=shutil.disk_usage(ROOT).free,
                )
            elif args.action == "query":
                from app.core.context import TenantContext
                from app.modules.reporting.queries import query_ads
                from app.modules.reporting.schemas import ReportingFilter

                context = TenantContext(
                    tenant_id=UUID(fixture["tenant_id"]),
                    actor_id=UUID(fixture["actor_id"]),
                    role="operator",
                )
                traces = []

                class TraceHandler(logging.Handler):
                    def emit(self, record):
                        try:
                            traces.append(json.loads(record.getMessage()))
                        except (ValueError, TypeError):
                            pass

                logger = logging.getLogger("app.query_trace")
                logger.setLevel(logging.INFO)
                handler = TraceHandler()
                logger.addHandler(handler)
                sql_stats = {"count": 0, "seconds": 0.0}
                plan_candidates = {}

                def before(conn, cursor, statement, params, context, many):
                    context._perf_started = time.perf_counter()

                def after(conn, cursor, statement, params, context, many):
                    elapsed = time.perf_counter() - context._perf_started
                    sql_stats["count"] += 1
                    sql_stats["seconds"] += elapsed
                    if (
                        args.explain
                        and statement.lstrip().upper().startswith("SELECT")
                        and any(
                            name in statement
                            for name in (
                                "ad_report_day",
                                "material_report_day",
                                "ad_object",
                                "material_post_link",
                            )
                        )
                    ):
                        key = hashlib.sha256(statement.encode()).hexdigest()
                        if elapsed > plan_candidates.get(key, {}).get("seconds", -1):
                            plan_candidates[key] = {
                                "statement": statement,
                                "params": params,
                                "seconds": elapsed,
                            }

                event.listen(engine, "before_cursor_execute", before)
                event.listen(engine, "after_cursor_execute", after)
                records = []
                result["queries"] = records
                for window in map(int, args.windows.split(",")):
                    if window > parameters.days:
                        continue
                    for dimension in args.dimensions.split(","):
                        for repeat in range(args.repeat):
                            filters = ReportingFilter(
                                dimension=dimension,
                                start_date=parameters.end_date
                                - timedelta(days=window - 1),
                                end_date=parameters.end_date,
                                advertiser_ids=tuple(fixture["accounts"]),
                                include_empty=args.include_empty,
                                sort_by=args.sort_by,
                                sort_direction="desc",
                                query=args.search,
                            )
                            record = {
                                "window": window,
                                "dimension": dimension,
                                "repeat": repeat,
                                "include_empty": args.include_empty,
                                "search": args.search,
                                "sort_by": args.sort_by,
                                "cache": "new_snapshot_db_cache_uncontrolled",
                            }
                            before_count = sql_stats.copy()
                            trace_start = len(traces)
                            case_started = time.perf_counter()
                            try:
                                with Session(engine) as session:
                                    start = time.perf_counter()
                                    page = query_ads(
                                        session,
                                        context=context,
                                        bc_id=fixture["bc_id"],
                                        filters=filters,
                                        limit=50,
                                    )
                                    record["first_ms"] = (
                                        time.perf_counter() - start
                                    ) * 1000
                                    record["total"] = page.total
                                    record["summary"] = page.summary
                                    record["page_digest"] = hashlib.sha256(
                                        json.dumps(
                                            page.model_dump(mode="json")["items"],
                                            sort_keys=True,
                                        ).encode()
                                    ).hexdigest()
                                    buckets = page.summary.get("buckets", [])
                                    spend = sum(
                                        (
                                            Decimal(str(b["values"]["spend"]))
                                            for b in buckets
                                            if b["values"].get("spend") is not None
                                        ),
                                        Decimal(0),
                                    )
                                    if not args.search:
                                        expected = expected_metrics(
                                            parameters,
                                            object_ids=range(
                                                1, parameters.active_ads + 1
                                            ),
                                            days=window,
                                        )["spend"]
                                        record["oracle_spend"] = str(expected)
                                        record["actual_spend"] = str(spend)
                                        if spend != expected:
                                            raise AssertionError(
                                                f"{dimension}/{window}: amount oracle mismatch {spend} != {expected}"
                                            )
                                    start = time.perf_counter()
                                    second = query_ads(
                                        session,
                                        context=context,
                                        bc_id=fixture["bc_id"],
                                        filters=filters,
                                        snapshot_id=page.snapshot.snapshot_id,
                                        cursor=page.next_cursor,
                                        limit=50,
                                    )
                                    record["page_ms"] = (
                                        time.perf_counter() - start
                                    ) * 1000
                                    record["page_rows"] = len(second.items)
                                    record["member_keys"] = [
                                        row.row_key for row in page.items
                                    ]
                                    session.commit()
                            except Exception as error:
                                record["failure_type"] = type(error).__name__
                                record["sqlstate"] = getattr(
                                    getattr(error, "orig", None), "sqlstate", None
                                )
                                record["elapsed_ms"] = (
                                    time.perf_counter() - case_started
                                ) * 1000
                                result["query_failures"] = (
                                    result.get("query_failures", 0) + 1
                                )
                                if not args.continue_on_error:
                                    records.append(record)
                                    raise
                            record["sql_count"] = (
                                sql_stats["count"] - before_count["count"]
                            )
                            record["sql_ms"] = (
                                sql_stats["seconds"] - before_count["seconds"]
                            ) * 1000
                            record["traces"] = traces[trace_start:]
                            records.append(record)
                            result["max_rss_bytes"] = rss_bytes()
                            write(args.output, result)
                logger.removeHandler(handler)
                if args.explain:
                    result["explain_plans"] = []
                    from contextlib import closing

                    with closing(engine.raw_connection()) as raw_conn:
                        with raw_conn.cursor() as cur:
                            cur.execute("SET TRANSACTION READ ONLY")
                            cur.execute("SET LOCAL statement_timeout='15s'")
                            for candidate in sorted(
                                plan_candidates.values(),
                                key=lambda x: x["seconds"],
                                reverse=True,
                            )[:3]:
                                cur.execute(
                                    "EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) "
                                    + candidate["statement"],
                                    candidate["params"],
                                )
                                result["explain_plans"].append(
                                    {
                                        "sql": candidate["statement"],
                                        "plan": cur.fetchone()[0],
                                    }
                                )
                        raw_conn.rollback()
            elif args.action == "rebuild":
                if args.code != "current":
                    raise ValueError("read models must be rebuilt by current code")
                from app.modules.reporting.read_models.projector import rebuild_pending
                from app.modules.reporting.read_models.rebuild import (
                    backfill_directory_page,
                    enqueue_fact_buckets,
                )

                started = time.perf_counter()
                counts = {
                    "directory": 0,
                    "naming": 0,
                    "usage": 0,
                    "fact_buckets": 0,
                    "projected": 0,
                }
                with Session(engine) as session:
                    for advertiser in fixture["accounts"]:
                        for kind in ("directory", "naming", "usage"):
                            cursor = None
                            while True:
                                page = backfill_directory_page(
                                    session,
                                    tenant_id=UUID(fixture["tenant_id"]),
                                    advertiser_id=advertiser,
                                    kind=kind,
                                    after=cursor,
                                    limit=500,
                                )
                                session.commit()
                                counts[kind] += page.processed
                                cursor = page.after
                                if cursor is None:
                                    break
                        counts["fact_buckets"] += enqueue_fact_buckets(
                            session,
                            tenant_id=UUID(fixture["tenant_id"]),
                            advertiser_id=advertiser,
                            start=parameters.end_date
                            - timedelta(days=parameters.days - 1),
                            end=parameters.end_date,
                        )
                        session.commit()
                    while True:
                        pages = rebuild_pending(session, limit=25)
                        session.commit()
                        counts["projected"] += len(pages)
                        if not pages:
                            break
                result.update(
                    rebuild_counts=counts, rebuild_seconds=time.perf_counter() - started
                )
            elif args.action == "publish":
                from app.modules.reporting.publish import AccountMeta, publish_ad_day
                from tests.modules.reporting.sync_conftest import ad_row

                # 使用独立未来日避免覆盖用于旧新查询对照的90日源事实。
                target = parameters.end_date + timedelta(days=1)
                rows = []
                advertiser = fixture["accounts"][0]
                for i in range(1, args.objects + 1):
                    metric = synthetic_fact_for_publish(parameters, i)
                    rows.append(
                        ad_row(
                            f"publish-{i}",
                            target,
                            campaign=f"publish-c-{i // 10}",
                            adv=advertiser,
                            spend=str(metric["spend"]),
                            d0=str(metric["revenue"]),
                            total=str(metric["revenue"]),
                        )
                    )
                with Session(engine) as session:
                    started = time.perf_counter()
                    publish_ad_day(
                        session,
                        tenant_id=UUID(fixture["tenant_id"]),
                        job_id=uuid4(),
                        accounts={advertiser: AccountMeta("USD", "UTC")},
                        start=target,
                        end=target,
                        rows=rows,
                    )
                    session.commit()
                    result["publish_seconds"] = time.perf_counter() - started
                    result["rows"] = len(rows)
                    actual = session.execute(
                        text(
                            "SELECT sum(spend) FROM ad_report_day WHERE tenant_id=:tenant AND advertiser_id=:adv AND stat_date=:day"
                        ),
                        {
                            "tenant": UUID(fixture["tenant_id"]),
                            "adv": advertiser,
                            "day": target,
                        },
                    ).scalar_one()
                    expected = sum(
                        (Decimal(r["metrics"]["spend"]) for r in rows), Decimal(0)
                    )
                    if actual != expected:
                        raise AssertionError("published amount differs from oracle")
                    result["amount_checked"] = str(actual)
        result["matrix_finished"] = True
        result["complete"] = result.get("query_failures", 0) == 0
    except Exception as error:
        result["failure_type"] = type(error).__name__
        raise
    finally:
        result["finished_at"] = time.time()
        result["max_rss_bytes"] = rss_bytes()
        result["free_disk_after"] = shutil.disk_usage(ROOT).free
        write(args.output, result)


def synthetic_fact_for_publish(parameters, i):
    from tests.performance.sync_query_fixture import synthetic_fact

    return synthetic_fact(parameters, i, 0)


if __name__ == "__main__":
    main()
