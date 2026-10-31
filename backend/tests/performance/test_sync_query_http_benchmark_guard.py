"""纯参数/guard测试；使用 --noconftest 且禁自动插件，不连接数据库或启动服务。"""

import builtins
import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def tool():
    path = Path(__file__).resolve().parents[3] / "scripts/sync-query-http-benchmark.py"
    spec = importlib.util.spec_from_file_location("http_benchmark_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated(monkeypatch):
    module = tool()
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://127.0.0.1:5432/sync_redesign_test_perf"
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:59379/12")
    monkeypatch.setattr(
        module.shutil, "disk_usage", lambda _: SimpleNamespace(free=11 * 1024**3)
    )
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://127.0.0.1/business",
        "postgresql+psycopg://remote.invalid/sync_redesign_test_perf",
        "postgresql+psycopg://127.0.0.1:5433/sync_redesign_test_perf",
        "postgresql+psycopg://127.0.0.1/sync_redesign_test_perf?host=remote.invalid",
        "postgresql+psycopg://127.0.0.1/sync_redesign_test_perf#options",
        "postgresql+psycopg://127.0.0.1/sync_redesign_test_root",
        "postgresql+psycopg://127.0.0.1:bad/sync_redesign_test_perf",
        "sqlite:///sync_redesign_test_perf",
    ],
)
def test_database_rejected_before_any_app_import(isolated, monkeypatch, tmp_path, url):
    monkeypatch.setenv("DATABASE_URL", url)
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "app" or name.startswith("app."):
            pytest.fail("guard imported application before refusal")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    output = tmp_path / "new.json"
    with pytest.raises(ValueError):
        isolated.main(
            ["--manifest", str(tmp_path / "absent.json"), "--output", str(output)]
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "url",
    [
        "redis://127.0.0.1:6379/0",
        "redis://127.0.0.1:59379/0",
        "redis://localhost:59379/12",
        "redis://remote.invalid:59379/12",
        "redis://127.0.0.1:59379/12?db=0",
        "redis://127.0.0.1:59379/12#db",
        "redis://user:secret@127.0.0.1:59379/12",
        "rediss://127.0.0.1:59379/12",
        "redis://127.0.0.1:59380/9",
    ],
)
def test_shared_remote_or_overridden_redis_rejected(isolated, monkeypatch, url):
    monkeypatch.setenv("REDIS_URL", url)
    with pytest.raises(ValueError, match="dedicated local Redis"):
        isolated.guard()


def test_exact_fixture_pairs_empty_environment_and_disk(isolated, monkeypatch):
    isolated.guard()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://127.0.0.1:5432/sync_redesign_test_concurrency",
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:59380/9")
    isolated.guard()
    monkeypatch.setenv("APP_ENV_FILE", "/some/business.env")
    with pytest.raises(ValueError, match="APP_ENV_FILE"):
        isolated.guard()
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setattr(
        isolated.shutil, "disk_usage", lambda _: SimpleNamespace(free=9 * 1024**3)
    )
    with pytest.raises(ValueError, match="10 GiB"):
        isolated.guard()


def test_server_environment_drops_credentials_proxy_and_connection_overrides():
    module = tool()
    source = {
        "PATH": "/bin",
        "DATABASE_URL": "local-db",
        "REDIS_URL": "local-redis",
        "SECRET_KEY": "old-key",
        "PGHOST": "remote",
        "PGSERVICE": "real",
        "HTTPS_PROXY": "remote",
        "HTTP_PROXY": "remote",
        "SENTRY_DSN": "remote",
        "S3_SECRET_ACCESS_KEY": "real",
        "TIKTOK_APP_SECRET": "real",
        "APP_ENV_FILE": "real",
        "FIRST_SUPERUSER_PASSWORD": "real",
        **dict.fromkeys(module.SWITCHES, "true"),
    }
    env = module.isolated_environment(source, secret="temporary-only")
    assert env["SECRET_KEY"] == "temporary-only"
    assert env["APP_ENV_FILE"] == "/dev/null"
    assert all(env[key] == "false" for key in module.SWITCHES)
    assert env["DATABASE_URL"] == "local-db"
    assert env["REDIS_URL"] == "local-redis"
    assert not any(
        key in env
        for key in (
            "PGHOST",
            "PGSERVICE",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "SENTRY_DSN",
            "TIKTOK_APP_SECRET",
            "S3_SECRET_ACCESS_KEY",
        )
    )
    assert "real" not in env.values()


@pytest.mark.parametrize(
    "options",
    [
        ["--port", "80"],
        ["--port", "-1"],
        ["--port", "65536"],
        ["--timeout", "0"],
        ["--timeout", "301"],
        ["--timeout", "nan"],
        ["--same-days", "0"],
        ["--same-days", "91"],
    ],
)
def test_invalid_parameters_stop_before_runtime(tmp_path, options):
    with pytest.raises(SystemExit):
        tool().parse_args(
            [
                "--manifest",
                "fixture.json",
                "--output",
                str(tmp_path / "new.json"),
                *options,
            ]
        )


def test_existing_or_dangling_output_not_overwritten(tmp_path):
    module = tool()
    output = tmp_path / "evidence.json"
    output.write_text("original evidence")
    with pytest.raises(SystemExit):
        module.parse_args(["--manifest", "fixture.json", "--output", str(output)])
    assert output.read_text() == "original evidence"
    output.unlink()
    output.symlink_to(tmp_path / "missing")
    with pytest.raises(SystemExit):
        module.parse_args(["--manifest", "fixture.json", "--output", str(output)])


def manifest():
    return {
        "parameters": {"generator_revision": 2, "accounts": 2},
        "tenant_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "bc_id": "bc-a",
        "accounts": ["perf-account-1", "perf-account-2"],
    }


@pytest.mark.parametrize("change", ["actor", "accounts", "revision", "bc"])
def test_manifest_requires_frozen_synthetic_identity(tmp_path, change):
    fixture = manifest()
    if change == "actor":
        fixture["actor_id"] = "real-user"
    elif change == "accounts":
        fixture["accounts"] = ["real-advertiser"]
    elif change == "revision":
        fixture["parameters"]["generator_revision"] = 1
    else:
        fixture["bc_id"] = "real-bc"
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError):
        tool().read_manifest(path)


def test_same_manifest_hash_and_real_http_route_parameters(tmp_path):
    module = tool()
    fixture = manifest()
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    actual, digest = module.read_manifest(path)
    assert actual == fixture and len(digest) == 64
    parameters = SimpleNamespace(end_date=date(2026, 10, 30))
    case = SimpleNamespace(dimension="account", days=7)
    route = module.request_path(fixture, parameters, case)
    assert route.startswith(f"/api/tenants/{fixture['tenant_id']}/ads?")
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(route).query)
    assert query["ids"] == ["perf-account-1,perf-account-2"]
    assert query["start_date"] == ["2026-10-24"]
    assert query["limit"] == ["50"]
    assert query["sort_by"] == ["spend"]


def fixture_page():
    buckets = [
        {
            "currency": "USD",
            "values": {"spend": "0.30", "revenue_d0": "0.61", "revenue_total": "0.61"},
        }
    ]
    page = {
        "summary": {"buckets": buckets},
        "total": 1,
        "items": [{"row_key": "member-a", "metric_buckets": buckets}],
        "snapshot": {"snapshot_id": str(uuid4())},
    }
    oracle = {
        "metrics": {"spend": Decimal("0.30"), "revenue": Decimal("0.61")},
        "total": 1,
        "keys": ["member-a"],
        "rows": {"member-a": {"spend": Decimal("0.30"), "revenue": Decimal("0.61")}},
    }
    return page, oracle


def test_decimal_and_member_oracle_checks_full_response_not_just_status():
    module = tool()
    page, oracle = fixture_page()
    assert module.validate_page(page, oracle)["complete"]
    page["items"][0]["row_key"] = "wrong-member"
    result = module.validate_page(page, oracle)
    assert not result["complete"] and not result["member_oracle_ok"]
    page, oracle = fixture_page()
    page["summary"]["buckets"][0]["values"]["revenue_total"] = "0.62"
    assert not module.validate_page(page, oracle)["amount_oracle_ok"]


@pytest.mark.parametrize("status", [202, 302, 401, 500])
def test_non_200_is_incomplete_and_does_not_expose_body(monkeypatch, status):
    module = tool()
    monkeypatch.setattr(
        module, "http_get", lambda *a, **kw: (status, b"sensitive-token-or-diagnostic")
    )
    result = module.http_case(
        1,
        "private-token",
        "/api",
        SimpleNamespace(dimension="ad", days=1),
        {},
        SimpleNamespace(wait=lambda **kw: None),
        1,
    )
    assert result["http_status"] == status and not result["complete"]
    assert "sensitive" not in json.dumps(result) and "private-token" not in json.dumps(
        result
    )
    assert result["elapsed_ms"] >= 0


def test_exception_type_only_and_timeout_retained(monkeypatch):
    module = tool()

    def fail(*_args, **_kwargs):
        raise TimeoutError("secret-token secret-db")

    monkeypatch.setattr(module, "http_get", fail)
    result = module.http_case(
        1,
        "token",
        "/api",
        SimpleNamespace(dimension="ad", days=1),
        {},
        SimpleNamespace(wait=lambda **kw: None),
        1,
    )
    assert result["failure_type"] == "TimeoutError"
    assert not result["complete"] and result["elapsed_ms"] >= 0
    assert "secret" not in json.dumps(result)


def test_existing_reference_workloads_and_oracles_are_pure():
    module = tool()
    reference = module.load_reference()
    cases = reference.distinct_cases(90, 20)
    assert len(set(cases)) == 20
    assert {case.dimension for case in cases} == set(reference.DIMENSIONS)
    # 只导入固定夹具的整数oracle，不导入app或连接数据库。
    backend = str(Path(__file__).resolve().parents[2])
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from tests.performance.sync_query_fixture import FixtureParameters

    parameters = FixtureParameters(objects=100, days=7, accounts=5)
    fixture = {"accounts": [f"perf-account-{i}" for i in range(1, 6)], "bc_id": "bc-a"}
    totals = []
    for dimension in reference.DIMENSIONS:
        oracle = module.expected_page(
            reference, parameters, fixture, reference.QueryCase(dimension, 7)
        )
        assert oracle["total"] >= len(oracle["keys"])
        assert len(oracle["keys"]) == len(set(oracle["keys"]))
        totals.append(oracle["metrics"])
    assert all(total == totals[0] for total in totals)


def test_server_failure_terminates_only_own_process_and_closes_socket(monkeypatch):
    module = tool()
    calls = []
    sock = SimpleNamespace(
        fileno=lambda: 77,
        getsockname=lambda: ("127.0.0.1", 54321),
        close=lambda: calls.append("close"),
    )
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: calls.append("terminate"),
        wait=lambda **kw: calls.append("wait"),
    )
    monkeypatch.setattr(module, "reserve_socket", lambda _: sock)

    def launch(command, **kwargs):
        assert command[-2:] == ["--serve-fd", "77"]
        assert kwargs["pass_fds"] == (77,)
        assert kwargs["stdout"] == kwargs["stderr"] == module.subprocess.DEVNULL
        assert "token" not in " ".join(command)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module, "http_get", lambda *a, **kw: (200, b"true"))
    with pytest.raises(RuntimeError):
        with module.local_server({"SECRET_KEY": "temporary-only"}) as port:
            assert port == 54321
            raise RuntimeError("simulated workload failure")
    assert calls == ["terminate", "wait", "close"]


def test_failure_result_is_persisted_without_overwriting_other_file(tmp_path):
    module = tool()
    output = tmp_path / "new.json"
    with output.open("x") as handle:
        module.persist(
            handle,
            {
                "complete": False,
                "failure_type": "TimeoutError",
                "http_measured": True,
                "full_scale": False,
                "slo_qualified": False,
            },
        )
    result = json.loads(output.read_text())
    assert result["failure_type"] == "TimeoutError"
    assert (
        not result["complete"]
        and not result["full_scale"]
        and not result["slo_qualified"]
    )


def test_singleflight_requires_same_returned_id_and_exactly_one_new_snapshot():
    module = tool()
    queries = [{"snapshot_id": "one", "complete": True}] * 20
    assert module.snapshot_check(queries, {"one"}, 1)["complete"]
    assert not module.snapshot_check(queries, {"one", "hidden-duplicate"}, 1)[
        "complete"
    ]
    assert not module.snapshot_check(queries, {"different"}, 1)["complete"]
    assert not module.snapshot_check(queries, set(), 1)["complete"]
    assert not module.snapshot_check(
        [{"snapshot_id": "one", "complete": False}], {"one"}, 1
    )["complete"]
    distinct = [{"snapshot_id": str(i), "complete": True} for i in range(20)]
    assert module.snapshot_check(distinct, {str(i) for i in range(20)}, 20)["complete"]


def test_transport_uses_fixed_loopback_and_reads_full_body_without_redirect(
    monkeypatch,
):
    module = tool()
    calls = []
    response = SimpleNamespace(
        status=302, read=lambda size: calls.append(("read", size)) or b"redirect"
    )

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def request(self, method, path, headers):
            calls.append(("request", method, path, headers))

        def getresponse(self):
            return response

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(module.http.client, "HTTPConnection", Connection)
    assert module.http_get(54321, "/api/test", token="private", timeout=2) == (
        302,
        b"redirect",
    )
    assert calls[0] == ("connect", "127.0.0.1", 54321, 2)
    assert calls[1][1:3] == ("GET", "/api/test")
    assert calls[1][3]["Authorization"] == "Bearer private"
    assert calls[2] == ("read", module.MAX_BODY + 1)
    assert calls[-1] == "closed"


def test_full_http_timer_stops_before_oracle_validation(monkeypatch):
    module = tool()
    page, oracle = fixture_page()
    clock = iter([1.0, 2.0, 2.125])
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(
        module, "http_get", lambda *a, **kw: (200, json.dumps(page).encode())
    )
    result = module.http_case(
        1,
        "private",
        "/api",
        SimpleNamespace(dimension="ad", days=1),
        oracle,
        SimpleNamespace(wait=lambda **kw: None),
        1,
    )
    assert result["complete"] and result["elapsed_ms"] == 125.0
    assert result["http_status"] == 200


def test_listener_validation_works_when_acceptconn_is_not_supported():
    module = tool()
    calls = []

    def option(level, name):
        calls.append((level, name))
        if name == module.socket.SO_ACCEPTCONN:
            raise OSError(42, "Protocol not available")
        return module.socket.SOCK_STREAM

    sock = SimpleNamespace(
        family=module.socket.AF_INET,
        getsockname=lambda: ("127.0.0.1", 54321),
        getsockopt=option,
        listen=lambda backlog: calls.append(("listen", backlog)),
    )
    module.validate_listener(sock)
    assert calls == [(module.socket.SOL_SOCKET, module.socket.SO_TYPE), ("listen", 128)]


@pytest.mark.parametrize("host,kind", [("0.0.0.0", 1), ("127.0.0.1", 2)])
def test_listener_validation_rejects_non_loopback_and_datagram(host, kind):
    module = tool()
    sock = SimpleNamespace(
        family=module.socket.AF_INET,
        getsockname=lambda: (host, 54321),
        getsockopt=lambda *_: kind,
    )
    with pytest.raises(ValueError):
        module.validate_listener(sock)


@pytest.mark.parametrize(
    "exit_code,reason",
    [
        (21, "child_listener_invalid"),
        (22, "child_application_import_failed"),
        (2, "child_exited_before_ready"),
    ],
)
def test_child_startup_failure_reports_static_reason_without_raw_logs(
    monkeypatch, exit_code, reason
):
    module = tool()
    sock = SimpleNamespace(
        fileno=lambda: 77, getsockname=lambda: ("127.0.0.1", 54321), close=lambda: None
    )
    process = SimpleNamespace(poll=lambda: exit_code)
    monkeypatch.setattr(module, "reserve_socket", lambda _: sock)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: process)
    with pytest.raises(module.BenchmarkFailure) as error:
        with module.local_server({}):
            pytest.fail("exited child cannot be ready")
    assert error.value.reason == reason


@pytest.mark.parametrize("flags", [["--mixed"], ["--allow-fixture-publish"]])
def test_mixed_publish_requires_both_explicit_flags(tmp_path, flags):
    with pytest.raises(SystemExit):
        tool().parse_args(
            [
                "--manifest",
                "fixture.json",
                "--output",
                str(tmp_path / "new.json"),
                *flags,
            ]
        )


def test_default_is_readonly_and_explicit_mixed_keeps_original_four_rounds(tmp_path):
    module = tool()
    args = ["--manifest", "fixture.json", "--output", str(tmp_path / "new.json")]
    pure = module.parse_args(args)
    assert not pure.mixed and not pure.allow_fixture_publish
    mixed = module.parse_args([*args, "--mixed", "--allow-fixture-publish"])
    assert mixed.mixed and mixed.allow_fixture_publish
    reference = module.load_reference()
    parameters = SimpleNamespace(days=90)
    original = module.workloads(reference, parameters, 7, mixed=False)
    extended = module.workloads(reference, parameters, 7, mixed=True)
    assert len(original) == 4 and len(extended) == 8
    assert original == extended[:4]
    assert sum(len(work) for _, work, _ in original) == 46
    assert sum(len(work) for _, work, _ in extended) == 92
    assert sum(publish for _, _, publish in extended) == 4
    assert [work for _, work, _ in original] == [work for _, work, _ in extended[4:]]


@pytest.mark.parametrize("unknown", [False, True])
def test_publish_preflight_is_scoped_to_first_account_and_original_future_day(unknown):
    module = tool()
    fixture = manifest()
    parameters = SimpleNamespace(end_date=date(2026, 10, 30))
    captured = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, _statement, values):
            captured.append(values)
            return SimpleNamespace(scalar_one=lambda: unknown)

    engine = SimpleNamespace(connect=Connection)
    if unknown:
        with pytest.raises(module.BenchmarkFailure) as error:
            module.validate_publish_target(engine, fixture, parameters)
        assert error.value.reason == "fixture_publish_target_contains_undeclared_data"
    else:
        result = module.validate_publish_target(engine, fixture, parameters)
        assert result["day"] == "2026-11-01" and not result["undeclared_data"]
    assert len(captured) == 1
    assert captured[0]["advertiser"] == "perf-account-1"
    assert captured[0]["day"] == date(2026, 11, 1)
    assert str(captured[0]["tenant"]) == fixture["tenant_id"]
    assert captured[0]["declared_ids"] == [
        f"concurrency-publish-{i}" for i in range(1000)
    ]
    assert parameters.end_date == date(2026, 10, 30)


@pytest.mark.parametrize("mixed", [False, True])
def test_only_mixed_worker_invokes_original_publisher_on_shared_barrier(
    monkeypatch, mixed
):
    module = tool()
    reference = module.load_reference()
    case = reference.QueryCase("material", 7)
    fixture, parameters, engine = {"token": "unchanged-fixture"}, object(), object()
    calls, parties = [], []
    publication = {
        "complete": True,
        "elapsed_ms": 125.0,
        "actual_rows": 1000,
        "actual_spend": "1250",
        "actual_revenue_d0": "2500",
        "actual_revenue_total": "2500",
    }

    def publisher(actual_engine, actual_fixture, actual_parameters, gate):
        assert mixed, "pure HTTP cannot publish"
        assert (
            actual_engine is engine
            and actual_fixture is fixture
            and actual_parameters is parameters
        )
        calls.append("publish")
        gate.wait(timeout=2)
        return publication

    def http(_port, _token, _path, _case, _oracle, barrier, _timeout):
        parties.append(barrier.parties)
        barrier.wait(timeout=2)
        return {"complete": True, "elapsed_ms": 1.0}

    monkeypatch.setattr(reference, "publish_future", publisher)
    monkeypatch.setattr(module, "http_case", http)
    monkeypatch.setattr(module, "request_path", lambda *_: "/api/fixed")
    queries, actual, timing = module.run_workload(
        reference,
        engine,
        fixture,
        parameters,
        port=1,
        token="private",
        work=[case] * 5,
        oracles={case: {}},
        timeout=2,
        mixed=mixed,
    )
    assert len(queries) == 5 and parties == [5 + int(mixed)] * 5
    assert calls == (["publish"] if mixed else [])
    if mixed:
        assert actual == {**publication, "sql_instrumented": False}
        assert "sql_instrumented" not in publication
        assert (
            timing["publication_finished_monotonic"]
            >= timing["publication_started_monotonic"]
        )
    else:
        assert actual is None and timing is None


def test_publish_failure_aborts_barrier_and_preserves_failure_type():
    module = tool()
    calls = []

    def fail(*_):
        raise RuntimeError("secret configuration")

    publication, timing = module.publish_with_timing(
        SimpleNamespace(publish_future=fail),
        None,
        {},
        None,
        SimpleNamespace(abort=lambda: calls.append("abort")),
    )
    assert calls == ["abort"]
    assert publication == {
        "kind": "publish",
        "complete": False,
        "failure_type": "RuntimeError",
        "sql_instrumented": False,
    }
    assert "started_monotonic" not in timing and "finished_monotonic" in timing
    assert "secret" not in json.dumps(publication)


def test_work_overlap_uses_actual_intervals_not_only_same_round_label():
    module = tool()
    queries = [
        {"http_started_monotonic": 1.0, "http_finished_monotonic": 3.0},
        {"http_started_monotonic": 5.0, "http_finished_monotonic": 6.0},
    ]
    overlap = module.overlap_evidence(
        queries, {"started_monotonic": 2.0, "finished_monotonic": 4.0}
    )
    assert overlap["overlap_ms_by_request"] == [1000.0, 0.0]
    assert overlap["overlapping_http_requests"] == 1
    assert module.round_complete(True, {"complete": True}, overlap)
    assert not module.round_complete(False, {"complete": True}, overlap)
    assert not module.round_complete(True, {"complete": False}, overlap)
    separated = module.overlap_evidence(
        queries, {"started_monotonic": 7.0, "finished_monotonic": 8.0}
    )
    assert not module.round_complete(True, {"complete": True}, separated)
    assert module.round_complete(True, None, None)


def test_publication_omits_unobserved_sql_without_mutating_reference_result():
    module = tool()
    raw = {
        "complete": True,
        "elapsed_ms": 12.5,
        "actual_rows": 1000,
        "actual_spend": "1250.00",
        "sql_count": 0,
        "sql_ms": 0.0,
        "traces": [],
    }
    normalized = module.normalize_publication(raw)
    assert normalized == {
        "complete": True,
        "elapsed_ms": 12.5,
        "actual_rows": 1000,
        "actual_spend": "1250.00",
        "sql_instrumented": False,
    }
    assert raw["sql_count"] == 0 and raw["sql_ms"] == 0.0 and raw["traces"] == []
    assert "sql_instrumented" not in raw
