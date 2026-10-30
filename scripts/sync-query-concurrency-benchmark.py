#!/usr/bin/env python3
"""隔离本地查询并发：真实冷构建、单飞及独立未来日报发布；不宣称HTTP/24h验收。"""

from __future__ import annotations

import argparse
import contextvars
import json
import logging
import math
import os
import resource
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
CASE = contextvars.ContextVar("benchmark_case", default=None)
DIMENSIONS = ("material", "ad", "account", "campaign", "adgroup", "drama")


def guard() -> None:
    """必须先于任何应用/settings导入；禁止URL query覆盖实际连接目标。"""
    url = urlsplit(os.environ.get("DATABASE_URL", ""))
    if (
        url.scheme not in {"postgresql", "postgresql+psycopg"}
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or (url.port or 5432) != 5432
        or url.path
        not in {"/sync_redesign_test_perf", "/sync_redesign_test_concurrency"}
        or url.query
        or url.fragment
    ):
        raise ValueError("requires the dedicated local perf database")
    if os.environ.get("APP_ENV_FILE") != "/dev/null":
        raise ValueError("APP_ENV_FILE must explicitly be /dev/null")
    if shutil.disk_usage(ROOT).free < 10 * 1024**3:
        raise ValueError("less than mandatory 10 GiB disk reserve")


@dataclass(frozen=True)
class QueryCase:
    dimension: str
    days: int
    query: str | None = None
    include_empty: bool = False
    sort_by: str = "spend"


def distinct_cases(days: int, count: int = 20) -> list[QueryCase]:
    if not 1 <= days <= 90 or not 1 <= count <= 6 * days:
        raise ValueError("case count must fit the fixture's distinct dimensions/dates")
    windows = list(
        dict.fromkeys([1, min(7, days), min(31, days), days, *range(1, days + 1)])
    )
    return [
        QueryCase(dimension, window) for window in windows for dimension in DIMENSIONS
    ][:count]


def distribution(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "p50_ms": ordered[math.ceil(len(ordered) * 0.5) - 1],
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_ms": ordered[-1],
    }


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str))
    temporary.replace(path)


def expire_fixture_snapshots(engine, fixture: dict) -> int:
    from sqlalchemy import text
    from sqlmodel import Session

    # 仅这一份合成fixture操作者；不清事实、不碰其他租户/actor、不伪造依赖水位。
    with Session(engine) as session:
        count = session.execute(
            text("""UPDATE query_snapshot SET expires_at=GREATEST(
                created_at+interval '1 microsecond',clock_timestamp()-interval '1 second')
                WHERE tenant_id=:tenant AND actor_id=:actor AND bc_id=:bc"""),
            {
                "tenant": UUID(fixture["tenant_id"]),
                "actor": UUID(fixture["actor_id"]),
                "bc": fixture["bc_id"],
            },
        ).rowcount
        session.commit()
    return count


class TraceCollector(logging.Handler):
    def emit(self, record):
        case = CASE.get()
        if case is not None:
            try:
                payload = json.loads(record.getMessage())
            except ValueError, TypeError:
                return
            if payload.get("event") == "report_query":
                case["traces"].append(payload)


def fixture_members(parameters, case: QueryCase) -> tuple[int, list[str]]:
    """独立整数/分数oracle；同时验证旧类型帖子、空目录与广告的成员和顺序。"""
    from fractions import Fraction

    from tests.performance.sync_query_fixture import expected_metrics

    def key(i, empty=False):
        adv = f"perf-account-{(i - 1) % parameters.accounts + 1}"
        if case.dimension == "ad":
            return f"{adv}:ad:ad-{i}"
        return json.dumps(
            ["material", f"video-{i}"] if empty else ["post", adv, f"post-{i}"]
        )

    def name(i):
        return f"perf-{'ad' if case.dimension == 'ad' else 'video'}-{i}"

    records = []
    for i in range(1, parameters.active_ads + 1):
        values = expected_metrics(parameters, object_ids=[i], days=case.days)
        value = (
            name(i)
            if case.sort_by == "name"
            else Fraction(values["revenue"]) / Fraction(values["spend"])
            if case.sort_by == "roas_d0"
            else values["spend"]
        )
        records.append((key(i), value))
    # 广告名称搜索会并入命中的无数据目录对象，不能用默认事实成员口径验证。
    if case.include_empty or (case.dimension == "ad" and case.query == "perf"):
        start = parameters.active_ads + 1 if case.dimension == "ad" else 1
        records.extend(
            (
                key(i, True),
                name(i)
                if case.sort_by == "name"
                else None
                if case.sort_by == "roas_d0"
                else Decimal(0),
            )
            for i in range(start, parameters.objects + 1)
        )
    records.sort(key=lambda item: item[0].encode())
    present = [item for item in records if item[1] is not None]
    present.sort(key=lambda item: item[1], reverse=True)
    ordered = present + [item for item in records if item[1] is None]
    return len(records), [item[0] for item in ordered[:50]]


def query_case(
    engine, fixture, parameters, case: QueryCase, barrier: threading.Barrier
) -> dict:
    from app.core.context import TenantContext
    from app.modules.reporting.queries import query_ads
    from app.modules.reporting.schemas import ReportingFilter
    from sqlmodel import Session
    from tests.performance.sync_query_fixture import expected_metrics

    result = {
        "dimension": case.dimension,
        "days": case.days,
        "search": case.query,
        "include_empty": case.include_empty,
        "sort_by": case.sort_by,
        "sql_count": 0,
        "sql_ms": 0.0,
        "traces": [],
        "complete": False,
    }
    token = CASE.set(result)
    started = time.perf_counter()
    try:
        filters = ReportingFilter(
            dimension=case.dimension,
            start_date=parameters.end_date - timedelta(days=case.days - 1),
            end_date=parameters.end_date,
            advertiser_ids=tuple(fixture["accounts"]),
            sort_by=case.sort_by,
            sort_direction="desc",
            query=case.query,
            include_empty=case.include_empty,
        )
        context = TenantContext(
            tenant_id=UUID(fixture["tenant_id"]),
            actor_id=UUID(fixture["actor_id"]),
            role="operator",
        )
        oracle = expected_metrics(
            parameters, object_ids=range(1, parameters.active_ads + 1), days=case.days
        )
        expected = oracle["spend"]
        member_oracle = (
            fixture_members(parameters, case)
            if case.dimension in {"material", "ad"}
            else None
        )
        barrier.wait(timeout=30)
        started = time.perf_counter()
        try:
            with Session(engine) as session:
                page = query_ads(
                    session,
                    context=context,
                    bc_id=fixture["bc_id"],
                    filters=filters,
                    limit=50,
                )
                result["elapsed_ms"] = (time.perf_counter() - started) * 1000
                actual = sum(
                    (
                        Decimal(str(bucket["values"]["spend"]))
                        for bucket in page.summary["buckets"]
                        if bucket["values"].get("spend") is not None
                    ),
                    Decimal(0),
                )
                revenue = {
                    name: sum(
                        (
                            Decimal(str(bucket["values"][name]))
                            for bucket in page.summary["buckets"]
                            if bucket["values"].get(name) is not None
                        ),
                        Decimal(0),
                    )
                    for name in ("revenue_d0", "revenue_total")
                }
                oracle_ok = actual == expected and all(
                    value == oracle["revenue"] for value in revenue.values()
                )
                result.update(
                    oracle_revenue=str(oracle["revenue"]),
                    actual_revenue={
                        name: str(value) for name, value in revenue.items()
                    },
                    snapshot_id=str(page.snapshot.snapshot_id),
                    total=page.total,
                    page_rows=len(page.items),
                    oracle_spend=str(expected),
                    actual_spend=str(actual),
                    oracle_ok=oracle_ok,
                )
                if not oracle_ok:
                    raise AssertionError("amount_oracle_mismatch")
                if member_oracle is not None:
                    result["member_oracle_ok"] = (
                        page.total == member_oracle[0]
                        and [row.row_key for row in page.items] == member_oracle[1]
                    )
                    if not result["member_oracle_ok"]:
                        raise AssertionError("member_or_order_oracle_mismatch")
                # query_ads只有完整SnapshotPage才成功；不把受理/未构建/HTTP202记为完成。
                result["complete"] = True
        except Exception as exc:  # noqa: BLE001 - 压测失败保留类型和失败结果，不输出连接/报表正文
            result.update(
                elapsed_ms=(time.perf_counter() - started) * 1000,
                failure_type=type(exc).__name__,
                sqlstate=getattr(getattr(exc, "orig", None), "sqlstate", None),
            )
        return result
    except Exception as exc:  # noqa: BLE001 - 压测失败保留类型和失败结果，不输出连接/报表正文
        result.update(
            elapsed_ms=(time.perf_counter() - started) * 1000,
            failure_type=type(exc).__name__,
        )
        return result
    finally:
        CASE.reset(token)


def publish_future(engine, fixture, parameters, barrier: threading.Barrier) -> dict:
    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from sqlalchemy import text
    from sqlmodel import Session

    target = parameters.end_date + timedelta(days=2)
    advertiser = fixture["accounts"][0]
    result = {
        "kind": "publish",
        "rows": 1000,
        "day": str(target),
        "sql_count": 0,
        "sql_ms": 0.0,
        "traces": [],
        "complete": False,
    }
    from tests.modules.reporting.sync_conftest import ad_row

    rows = [
        ad_row(
            f"concurrency-publish-{i}",
            target,
            adv=advertiser,
            campaign="concurrency-publish-c",
            adgroup="concurrency-publish-g",
            spend="1.25",
            d0="2.50",
            total="2.50",
        )
        for i in range(1000)
    ]
    token = CASE.set(result)
    started = time.perf_counter()
    try:
        barrier.wait(timeout=30)
        started = time.perf_counter()
        try:
            with Session(engine) as session:
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
                actual = session.execute(
                    text(
                        "SELECT count(*) AS n,sum(spend) AS spend,sum(revenue_d0) AS revenue_d0,sum(revenue_total) AS revenue_total FROM ad_report_day WHERE tenant_id=:tenant AND advertiser_id=:adv AND stat_date=:day"
                    ),
                    {
                        "tenant": UUID(fixture["tenant_id"]),
                        "adv": advertiser,
                        "day": target,
                    },
                ).one()
                result.update(
                    actual_rows=actual.n,
                    actual_spend=str(actual.spend),
                    actual_revenue_d0=str(actual.revenue_d0),
                    actual_revenue_total=str(actual.revenue_total),
                )
                if (
                    actual.n != 1000
                    or actual.spend != Decimal(1250)
                    or actual.revenue_d0 != Decimal(2500)
                    or actual.revenue_total != Decimal(2500)
                ):
                    raise AssertionError("publish_readback_mismatch")
                result["complete"] = True
        except Exception as exc:  # noqa: BLE001 - 压测失败保留类型和失败结果，不输出连接/报表正文
            result.update(
                failure_type=type(exc).__name__,
                sqlstate=getattr(getattr(exc, "orig", None), "sqlstate", None),
            )
        result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        return result
    except Exception as exc:  # noqa: BLE001 - 压测失败保留类型和失败结果，不输出连接/报表正文
        result.update(
            elapsed_ms=(time.perf_counter() - started) * 1000,
            failure_type=type(exc).__name__,
        )
        return result
    finally:
        CASE.reset(token)


def sample_resources(observer, stopped: threading.Event, samples: list) -> None:
    from sqlalchemy import text

    while not stopped.is_set():
        item = {
            "at": time.time(),
            "rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
        }
        try:
            with observer.connect() as connection:
                connection.execute(text("SET LOCAL statement_timeout='2s'"))
                item.update(
                    connection.execute(
                        text("""SELECT count(*) FILTER (WHERE pid<>pg_backend_pid()) AS connections,
                    count(*) FILTER (WHERE state='active' AND pid<>pg_backend_pid()) AS active,
                    count(*) FILTER (WHERE wait_event_type='Lock') AS lock_waiters
                    FROM pg_stat_activity WHERE datname=current_database()""")
                    )
                    .mappings()
                    .one()
                )
                item.update(
                    connection.execute(
                        text(
                            "SELECT temp_bytes,blks_read,blks_hit FROM pg_stat_database WHERE datname=current_database()"
                        )
                    )
                    .mappings()
                    .one()
                )
        except Exception as exc:  # noqa: BLE001 - 压测失败保留类型和失败结果，不输出连接/报表正文
            item["failure_type"] = type(exc).__name__
        samples.append(item)
        stopped.wait(0.25)


def run_round(
    engine,
    observer,
    fixture,
    parameters,
    *,
    name: str,
    cases: list[QueryCase],
    mixed: bool,
) -> dict:
    expired = expire_fixture_snapshots(engine, fixture)
    samples = []
    stop = threading.Event()
    monitor = threading.Thread(
        target=sample_resources, args=(observer, stop, samples), daemon=True
    )
    barrier = threading.Barrier(len(cases) + int(mixed))
    started = time.perf_counter()
    monitor.start()
    try:
        with ThreadPoolExecutor(max_workers=barrier.parties) as pool:
            futures = [
                pool.submit(query_case, engine, fixture, parameters, case, barrier)
                for case in cases
            ]
            publish = (
                pool.submit(publish_future, engine, fixture, parameters, barrier)
                if mixed
                else None
            )
            results = [future.result() for future in futures]
            publication = publish.result() if publish else None
    finally:
        stop.set()
        monitor.join(timeout=5)
    builds = sum(
        1
        for result in results
        for trace in result["traces"]
        for stage in trace.get("stages", [])
        if stage["name"] == "snapshot_write" and stage["outcome"] == "success"
    )
    expected_builds = len(set(cases))
    complete = (
        all(result["complete"] for result in results)
        and builds == expected_builds
        and len({r.get("snapshot_id") for r in results if r["complete"]})
        == expected_builds
        and (publication is None or publication["complete"])
        and bool(samples)
        and not any("failure_type" in sample for sample in samples)
    )
    return {
        "name": name,
        "concurrency": len(cases),
        "mixed_publish": mixed,
        "expired_snapshots": expired,
        "wall_ms": (time.perf_counter() - started) * 1000,
        "complete": complete,
        "expected_builds": expected_builds,
        "actual_builds": builds,
        "distinct_snapshot_count": len(
            {r.get("snapshot_id") for r in results if r["complete"]}
        ),
        "latency": distribution([r["elapsed_ms"] for r in results if r["complete"]]),
        "latency_by_days": {
            str(days): distribution(
                [
                    r["elapsed_ms"]
                    for r in results
                    if r["complete"] and r["days"] == days
                ]
            )
            for days in sorted({case.days for case in cases})
        },
        "failed_requests": sum(not r["complete"] for r in results),
        "queries": results,
        "publication": publication,
        "resource_samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--distinct-count", type=int, default=20)
    parser.add_argument("--same-concurrency", default="1,5,20")
    parser.add_argument("--same-days", type=int, default=7)
    parser.add_argument("--mixed", action="store_true")
    parser.add_argument("--include-empty", action="store_true")
    # 合成夹具每个授权账户名称都含perf，搜索路径仍能独立核对全量金额oracle。
    parser.add_argument("--search", choices=["perf"])
    parser.add_argument(
        "--sort-by", choices=["spend", "roas_d0", "name"], default="spend"
    )
    args = parser.parse_args()
    guard()
    levels = [int(n) for n in args.same_concurrency.split(",")]
    if any(n not in {1, 5, 20} for n in levels):
        parser.error("same-concurrency must use 1,5,20")
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import settings
    from app.core.db import engine
    from sqlalchemy import create_engine, event
    from tests.performance.sync_query_fixture import FixtureParameters

    fixture = json.loads(args.manifest.read_text())
    p = fixture["parameters"]
    parameters = FixtureParameters(
        **{
            k: p[k]
            for k in (
                "objects",
                "seed",
                "days",
                "accounts",
                "active_fraction",
                "json_bytes",
            )
        },
        end_date=date.fromisoformat(p["end_date"]),
    )
    cases = distinct_cases(parameters.days, args.distinct_count)
    cases = [
        replace(
            case,
            query=args.search,
            include_empty=args.include_empty,
            sort_by=args.sort_by,
        )
        for case in cases
    ]
    same = QueryCase(
        "material",
        min(args.same_days, parameters.days),
        args.search,
        args.include_empty,
        args.sort_by,
    )
    if same.days < 1:
        parser.error("same-days must be positive")
    observer = create_engine(str(settings.DATABASE_URL), pool_size=1, max_overflow=0)
    logger = logging.getLogger("app.query_trace")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    collector = TraceCollector()
    logger.addHandler(collector)

    def before(_conn, _cursor, _statement, _params, context, _many):
        context._benchmark_started = time.perf_counter()
        case = CASE.get()
        if case is not None:
            case["sql_count"] += 1

    def after(_conn, _cursor, _statement, _params, context, _many):
        case = CASE.get()
        if case is not None:
            case["sql_ms"] += (time.perf_counter() - context._benchmark_started) * 1000

    def failed(error_context):
        case = CASE.get()
        context = error_context.execution_context
        if (
            case is not None
            and context is not None
            and hasattr(context, "_benchmark_started")
        ):
            case["sql_ms"] += (time.perf_counter() - context._benchmark_started) * 1000

    event.listen(engine, "handle_error", failed)
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    result = {
        "fixture": str(args.manifest),
        "started_at": time.time(),
        "complete": False,
        "slo_qualified": False,
        "http_measured": False,
        "cache_state": "fresh application snapshots; OS/PG caches not evicted",
        "pool_size": settings.DATABASE_POOL_SIZE,
        "max_overflow": settings.DATABASE_MAX_OVERFLOW,
        "observer_connection_budget": 1,
        "rounds": [],
    }
    try:
        workloads = [("distinct-cold", cases)] + [
            (f"same-{n}", [same] * n) for n in levels
        ]
        for mixed in [False, True] if args.mixed else [False]:
            for name, work in workloads:
                guard()
                result["rounds"].append(
                    run_round(
                        engine,
                        observer,
                        fixture,
                        parameters,
                        name=name,
                        cases=work,
                        mixed=mixed,
                    )
                )
                write(args.output, result)
        result["complete"] = all(r["complete"] for r in result["rounds"])
    finally:
        result["finished_at"] = time.time()
        write(args.output, result)
        event.remove(engine, "handle_error", failed)
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
        logger.removeHandler(collector)
        observer.dispose()
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
