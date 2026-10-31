#!/usr/bin/env python3
"""真实本机HTTP查询验收：正式认证/路由、固定合成oracle；不代表全规模SLO。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 32 * 1024**2
SWITCHES = (
    "ADS_SYNC_ENABLED",
    "AUTOMATED_RULES_ENABLED",
    "MATERIAL_INGEST_ENABLED",
    "MATERIAL_CLEANUP_ENABLED",
)
SERVER_FAILURES = {
    20: "child_environment_refused",
    21: "child_listener_invalid",
    22: "child_application_import_failed",
    23: "child_server_failed",
}


class BenchmarkFailure(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def guard() -> None:
    """先于任何app导入；URL选项、环境文件和共享Redis都不得改变实际目标。"""
    try:
        db = urlsplit(os.environ.get("DATABASE_URL", ""))
        redis = urlsplit(os.environ.get("REDIS_URL", ""))
        targets = {
            "/sync_redesign_test_perf": (59379, "/12"),
            "/sync_redesign_test_concurrency": (59380, "/9"),
        }
        database_ok = (
            db.scheme in {"postgresql", "postgresql+psycopg"}
            and db.hostname in {"127.0.0.1", "localhost", "::1"}
            and (db.port or 5432) == 5432
            and db.path in targets
            and not db.query
            and not db.fragment
        )
        redis_ok = (
            redis.scheme == "redis"
            and redis.hostname == "127.0.0.1"
            and (redis.port, redis.path) == targets.get(db.path)
            and redis.username is None
            and redis.password is None
            and not redis.query
            and not redis.fragment
        )
    except ValueError:
        raise ValueError("invalid isolated local target") from None
    if not database_ok:
        raise ValueError("requires dedicated local perf/concurrency database")
    if not redis_ok:
        raise ValueError("requires dedicated local Redis paired with database")
    if os.environ.get("APP_ENV_FILE") != "/dev/null":
        raise ValueError("APP_ENV_FILE must explicitly be /dev/null")
    if shutil.disk_usage(ROOT).free < 10 * 1024**3:
        raise ValueError("requires 10 GiB disk reserve")


def isolated_environment(source: dict[str, str], *, secret: str) -> dict[str, str]:
    # 不继承代理、PG连接覆盖、平台凭据、Sentry或对象存储配置；不生成/修改任何用户。
    result = {
        key: source[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        if key in source
    }
    result.update(
        APP_ENV_FILE="/dev/null",
        DATABASE_URL=source["DATABASE_URL"],
        REDIS_URL=source["REDIS_URL"],
        SECRET_KEY=secret,
        FIRST_SUPERUSER="http-benchmark-unused",
        FIRST_SUPERUSER_PASSWORD=secrets.token_urlsafe(32),
        PROJECT_NAME="isolated-sync-http-benchmark",
        PYTHONPATH=str(ROOT / "backend"),
        PYTHONUNBUFFERED="1",
    )
    for key in SWITCHES:
        result[key] = "false"
    # 只允许显式池大小，沿用正式Settings校验；不扩大默认15连接预算。
    for key in ("DATABASE_POOL_SIZE", "DATABASE_MAX_OVERFLOW"):
        if key in source:
            result[key] = source[key]
    return result


def load_reference():
    spec = importlib.util.spec_from_file_location(
        "sync_http_reference", ROOT / "scripts/sync-query-concurrency-benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_manifest(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    if len(raw) > 4 * 1024**2:
        raise ValueError("fixture manifest too large")
    fixture = json.loads(raw)
    p = fixture["parameters"]
    # 固定已提交生成器的身份合同；不能把任意业务actor借给临时签名者。
    if (
        p.get("generator_revision") != 2
        or fixture.get("bc_id") != "bc-a"
        or fixture.get("accounts")
        != [f"perf-account-{i}" for i in range(1, p["accounts"] + 1)]
    ):
        raise ValueError("requires revision-2 synthetic fixture manifest")
    for key in ("tenant_id", "actor_id"):
        if str(UUID(fixture[key])) != fixture[key]:
            raise ValueError("noncanonical fixture identity")
    return fixture, hashlib.sha256(raw).hexdigest()


def fixture_parameters(fixture):
    from tests.performance.sync_query_fixture import FixtureParameters

    p = fixture["parameters"]
    return FixtureParameters(
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


def verify_fixture_actor(engine, fixture) -> None:
    from app.models import User
    from app.modules.reporting.filters import authorized_grants
    from app.modules.tenants.models import Tenant, TenantMembership
    from app.modules.tenants.permissions import require_tenant
    from sqlmodel import Session

    actor, tenant = UUID(fixture["actor_id"]), UUID(fixture["tenant_id"])
    with Session(engine) as session:
        user = session.get(User, actor)
        workspace = session.get(Tenant, tenant)
        member = session.get(TenantMembership, (tenant, actor))
        if (
            user is None
            or workspace is None
            or member is None
            or user.username != f"sync-{actor.hex[:10]}"
            or user.hashed_password != "x"
            or user.is_superuser
            or workspace.name != f"t-{tenant.hex[:8]}"
            or member.role != "operator"
            or not member.active
        ):
            raise ValueError("manifest actor is not the unchanged synthetic fixture")
        context = require_tenant(
            session, actor_id=actor, tenant_id=tenant, action="read"
        )
        granted = {
            row.advertiser_id
            for row in authorized_grants(
                session, context=context, bc_id=fixture["bc_id"]
            )
        }
        if not set(fixture["accounts"]) <= granted or granted - set(
            fixture["accounts"]
        ) - {"acc-1", "acc-2"}:
            raise ValueError("fixture account authorization mismatch")


def snapshot_ids(engine, fixture) -> set[str]:
    from sqlalchemy import text

    with engine.connect() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT id FROM query_snapshot WHERE tenant_id=:tenant AND actor_id=:actor AND bc_id=:bc"
                ),
                {
                    "tenant": UUID(fixture["tenant_id"]),
                    "actor": UUID(fixture["actor_id"]),
                    "bc": fixture["bc_id"],
                },
            )
        }


def expected_page(reference, parameters, fixture, case) -> dict:
    """整数分oracle来自原夹具；分组身份独立计算，不调用生产查询构造结果。"""
    from tests.performance.sync_query_fixture import expected_metrics

    metrics = expected_metrics(
        parameters, object_ids=range(1, parameters.active_ads + 1), days=case.days
    )
    groups = {}
    for i in range(1, parameters.active_ads + 1):
        adv = f"perf-account-{(i - 1) % parameters.accounts + 1}"
        if case.dimension == "account":
            key = f"{adv}:account:{adv}"
        elif case.dimension in {"campaign", "adgroup", "drama"}:
            divisor = 5 if case.dimension == "adgroup" else 10
            n = (
                (i - 1) // (parameters.accounts * divisor) * parameters.accounts
                + (i - 1) % parameters.accounts
                + 1
            )
            kind = "adgroup" if case.dimension == "adgroup" else "campaign"
            key = (
                f'{fixture["bc_id"]}:drama:["campaign-{n}"]'
                if case.dimension == "drama"
                else f"{adv}:{kind}:{kind}-{n}"
            )
        elif case.dimension == "ad":
            key = f"{adv}:ad:ad-{i}"
        else:
            key = json.dumps(["post", adv, f"post-{i}"])
        value = expected_metrics(parameters, object_ids=[i], days=case.days)
        group = groups.setdefault(key, {"spend": Decimal(0), "revenue": Decimal(0)})
        for metric in group:
            group[metric] += value[metric]
    ordered = sorted(groups, key=lambda key: key.encode())
    ordered.sort(key=lambda key: groups[key]["spend"], reverse=True)
    if case.dimension == "account":
        empty = sorted(
            f"{adv}:account:{adv}"
            for adv in fixture["accounts"]
            if f"{adv}:account:{adv}" not in groups
        )
        ordered += empty
    if case.dimension in {"ad", "material"}:
        total, first = reference.fixture_members(parameters, case)
        if (len(ordered), ordered[:50]) != (total, first):
            raise AssertionError("independent member oracles disagree")
    return {
        "metrics": metrics,
        "total": len(ordered),
        "keys": ordered[:50],
        "rows": groups,
    }


def request_path(fixture, parameters, case) -> str:
    query = {
        "bc_id": fixture["bc_id"],
        "dimension": case.dimension,
        "start_date": str(parameters.end_date - timedelta(days=case.days - 1)),
        "end_date": str(parameters.end_date),
        "sort_by": "spend",
        "sort_direction": "desc",
        "limit": "50",
        "include_empty": "false",
    }
    # API支持单advertiser而非多选CSV；账户维度用正式ids筛选排除seed的两个空账户。
    if case.dimension == "account":
        query["ids"] = ",".join(fixture["accounts"])
    return f"/api/tenants/{fixture['tenant_id']}/ads?{urlencode(query)}"


def amount_totals(buckets) -> dict[str, Decimal]:
    values = {}
    for name in ("spend", "revenue_d0", "revenue_total"):
        total = Decimal(0)
        for bucket in buckets:
            raw = bucket["values"].get(name)
            if raw is not None:
                value = Decimal(str(raw))
                if not value.is_finite() or bucket["currency"] != "USD":
                    raise ValueError("invalid metric bucket")
                total += value
        values[name] = total
    return values


def validate_page(page, oracle) -> dict:
    actual = amount_totals(page["summary"]["buckets"])
    expected = {
        "spend": oracle["metrics"]["spend"],
        "revenue_d0": oracle["metrics"]["revenue"],
        "revenue_total": oracle["metrics"]["revenue"],
    }
    keys = [row["row_key"] for row in page["items"]]
    amounts_ok = actual == expected
    members_ok = (
        type(page["total"]) is int
        and page["total"] == oracle["total"]
        and keys == oracle["keys"]
    )
    rows_ok = True
    for row in page["items"]:
        group = oracle["rows"].get(
            row["row_key"], {"spend": Decimal(0), "revenue": Decimal(0)}
        )
        rows_ok &= amount_totals(row["metric_buckets"]) == {
            "spend": group["spend"],
            "revenue_d0": group["revenue"],
            "revenue_total": group["revenue"],
        }
    return {
        "snapshot_id": str(UUID(page["snapshot"]["snapshot_id"])),
        "total": page["total"],
        "page_rows": len(keys),
        "row_keys": keys,
        "oracle_spend": str(expected["spend"]),
        "actual_spend": str(actual["spend"]),
        "oracle_revenue": str(oracle["metrics"]["revenue"]),
        "actual_revenue": {k: str(v) for k, v in actual.items() if k != "spend"},
        "amount_oracle_ok": amounts_ok,
        "member_oracle_ok": members_ok,
        "row_amount_oracle_ok": rows_ok,
        "complete": amounts_ok and members_ok and rows_ok,
    }


def http_get(port: int, path: str, *, token: str | None, timeout: float):
    # 直接HTTPConnection不读取HTTP(S)_PROXY；host固定loopback，禁止重定向跟随。
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("HTTP response exceeds local benchmark bound")
        return response.status, body
    finally:
        connection.close()


def http_case(port, token, path, case, oracle, barrier, timeout) -> dict:
    result = {
        "dimension": case.dimension,
        "days": case.days,
        "complete": False,
        "http_status": None,
    }
    started = time.perf_counter()
    try:
        barrier.wait(timeout=30)
        started = time.perf_counter()
        status, body = http_get(port, path, token=token, timeout=timeout)
        # 含TCP连接、正式认证/路由、DB提交、JSON序列化和全部响应字节；oracle校验不计入HTTP。
        result.update(
            elapsed_ms=(time.perf_counter() - started) * 1000,
            http_status=status,
            response_bytes=len(body),
        )
        if status != 200:
            result["failure_type"] = "HTTPStatusFailure"
            return result
        page = json.loads(body, parse_float=Decimal)
        result.update(validate_page(page, oracle))
        if not result["complete"]:
            result["failure_type"] = "OracleMismatch"
    except Exception as error:  # noqa: BLE001 - 只存异常类型，不存token、URL、响应正文或连接配置。
        result.setdefault("elapsed_ms", (time.perf_counter() - started) * 1000)
        result["failure_type"] = type(error).__name__
    return result


def reserve_socket(port: int = 0) -> socket.socket:
    if type(port) is not int or port != 0 and not 1024 <= port <= 65535:
        raise ValueError("port must be 0 or an unprivileged local port")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 不设置REUSEPORT，保留socket直至child接管，消除探测端口后抢占的竞态。
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        return sock
    except BaseException:
        sock.close()
        raise


@contextmanager
def local_server(environment, *, port=0):
    sock = reserve_socket(port)
    process = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve-fd",
                str(sock.fileno()),
            ],
            env=environment,
            cwd=ROOT / "backend",
            pass_fds=(sock.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        actual_port = sock.getsockname()[1]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                raise BenchmarkFailure(
                    SERVER_FAILURES.get(exit_code, "child_exited_before_ready")
                )
            try:
                status, body = http_get(
                    actual_port, "/api/utils/health-check/", token=None, timeout=0.5
                )
                if status == 200 and body == b"true":
                    break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            raise BenchmarkFailure("child_readiness_timeout")
        yield actual_port
    finally:
        # 只管理自己启动的PID，不使用pkill、不碰其它服务；先正常SIGTERM排空。
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        sock.close()


def validate_listener(sock) -> None:
    if (
        sock.family != socket.AF_INET
        or sock.getsockname()[0] != "127.0.0.1"
        or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
    ):
        raise ValueError("requires inherited private loopback listener")
    # macOS声明SO_ACCEPTCONN但getsockopt可返回ENOPROTOOPT；listen对现有监听幂等，
    # 并拒绝已连接的socket。不依赖不支持的选项，也不改成连接其它既有服务。
    sock.listen(128)


def serve(fd: int) -> int:
    stage = 20
    try:
        guard()
        if any(os.environ.get(key) != "false" for key in SWITCHES):
            raise ValueError("isolated server requires disabled business switches")
        stage = 21
        sock = socket.socket(fileno=fd)
        validate_listener(sock)
        stage = 22
        sys.path.insert(0, str(ROOT / "backend"))
        import uvicorn
        from app.main import app

        stage = 23
        # 正式app当前无启动写入/Beat生命周期；不注册依赖override，不调用init_db。
        config = uvicorn.Config(
            app,
            access_log=False,
            log_config=None,
            log_level="critical",
            timeout_graceful_shutdown=15,
        )
        uvicorn.Server(config).run(sockets=[sock])
        return 0
    except BaseException:  # noqa: BLE001 - 仅通过固定退出码传回阶段，不保存raw子进程日志。
        return stage


def persist(handle, result):
    handle.seek(0)
    json.dump(result, handle, indent=2, default=str)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def snapshot_check(queries, new_ids: set[str], expected: int) -> dict:
    identities = {query["snapshot_id"] for query in queries if query.get("snapshot_id")}
    singleflight = len(new_ids) == expected and identities == new_ids
    return {
        "expected_snapshots": expected,
        "new_snapshot_count": len(new_ids),
        "response_snapshot_count": len(identities),
        "new_snapshot_ids": sorted(new_ids),
        "singleflight_ok": singleflight,
        "complete": all(query["complete"] for query in queries) and singleflight,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--same-days", type=int, default=7)
    args = parser.parse_args(argv)
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error("port must be 0 or 1024..65535")
    if not 1 <= args.timeout <= 300 or not 1 <= args.same_days <= 90:
        parser.error("timeout must be 1..300 and same-days 1..90")
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists; choose a new evidence file")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    guard()
    fixture, digest = read_manifest(args.manifest)
    environment = isolated_environment(
        dict(os.environ), secret=secrets.token_urlsafe(48)
    )
    os.environ.clear()
    os.environ.update(environment)
    sys.path.insert(0, str(ROOT / "backend"))
    result = {
        "http_measured": False,
        "full_scale": False,
        "slo_qualified": False,
        "complete": False,
        "manifest_sha256": digest,
        "fixture": str(args.manifest),
        "fixture_parameters": fixture["parameters"],
        "started_at": time.time(),
        "transport": "real loopback TCP HTTP; separate uvicorn process; fresh connection per request",
        "latency_scope": "TCP connect through complete response body, excluding client oracle validation",
        "cache_state": "fixture-actor snapshots expired each round; OS/PG caches not evicted",
        "authentication": "official short-lived JWT, active synthetic user and current tenant/account authorization",
        "rounds": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL从首次创建保护现有证据；本进程增量保存同一份结果，失败也保留。
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        engine = None
        stage = "application_import"
        try:
            persist(handle, result)
            from app.core.config import settings
            from app.core.db import engine
            from app.core.security import create_access_token

            stage = "fixture_parameters"
            parameters = fixture_parameters(fixture)
            reference = load_reference()
            cases = reference.distinct_cases(parameters.days, 20)
            same = reference.QueryCase("material", min(args.same_days, parameters.days))
            stage = "oracle_prepare"
            oracles = {
                case: expected_page(reference, parameters, fixture, case)
                for case in set(cases + [same])
            }
            stage = "fixture_authorization"
            verify_fixture_actor(engine, fixture)
            token = create_access_token(fixture["actor_id"], timedelta(minutes=30))
            result.update(
                pool_size=settings.DATABASE_POOL_SIZE,
                max_overflow=settings.DATABASE_MAX_OVERFLOW,
            )
            stage = "http_server_start"
            with local_server(environment, port=args.port) as port:
                result["port"] = port
                stage = "http_authentication"
                probe_path = request_path(fixture, parameters, same)
                status, _ = http_get(port, probe_path, token=None, timeout=args.timeout)
                result["unauthenticated_status"] = status
                if status != 401:
                    raise AssertionError(
                        "formal authentication did not reject missing bearer"
                    )
                for name, work in [
                    ("distinct-cold-20", cases),
                    *[(f"same-cold-{n}", [same] * n) for n in (1, 5, 20)],
                ]:
                    guard()
                    stage = "snapshot_expire"
                    expired = reference.expire_fixture_snapshots(engine, fixture)
                    before = snapshot_ids(engine, fixture)
                    barrier = threading.Barrier(len(work))
                    started = time.perf_counter()
                    stage = "http_round"
                    with ThreadPoolExecutor(max_workers=len(work)) as pool:
                        futures = [
                            pool.submit(
                                http_case,
                                port,
                                token,
                                request_path(fixture, parameters, case),
                                case,
                                oracles[case],
                                barrier,
                                args.timeout,
                            )
                            for case in work
                        ]
                        queries = [future.result() for future in futures]
                    result["http_measured"] = True
                    round_result = {
                        "name": name,
                        "concurrency": len(work),
                        "expired_snapshots": expired,
                        "queries": queries,
                        "complete": False,
                    }
                    result["rounds"].append(round_result)
                    # 即使后续只读快照核验失败，也保留所有已完成HTTP请求的原始证据。
                    persist(handle, result)
                    stage = "snapshot_verification"
                    new = snapshot_ids(engine, fixture) - before
                    expected = len(set(work))
                    round_result.update(
                        {
                            "name": name,
                            "concurrency": len(work),
                            "expired_snapshots": expired,
                            "wall_ms": (time.perf_counter() - started) * 1000,
                            **snapshot_check(queries, new, expected),
                            "latency_all_requests": reference.distribution(
                                [q["elapsed_ms"] for q in queries]
                            ),
                            "failed_requests": sum(not q["complete"] for q in queries),
                            "queries": queries,
                        }
                    )
                    persist(handle, result)
            result["complete"] = all(row["complete"] for row in result["rounds"])
        except BaseException as error:  # noqa: BLE001 - 任何失败保存安全摘要，绝不输出凭据/异常SQL。
            result["failure_type"] = type(error).__name__
            result["failure_stage"] = stage
            result["failure_reason"] = (
                error.reason
                if isinstance(error, BenchmarkFailure)
                else "stage_exception"
            )
            result["complete"] = False
        finally:
            if engine is not None:
                engine.dispose()
            result["finished_at"] = time.time()
            persist(handle, result)
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--serve-fd":
            raise SystemExit(serve(int(sys.argv[2])))

        def terminate_benchmark(_signal, _frame):
            raise InterruptedError("benchmark terminated")

        signal.signal(signal.SIGTERM, terminate_benchmark)
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - 启动阶段同样只打印静态类型，不回显配置或输入。
        print(f"HTTP benchmark refused: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(2) from None
