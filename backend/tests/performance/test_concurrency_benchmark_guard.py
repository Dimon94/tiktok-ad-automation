"""并发压测只读导入与固定隔离数据库防护。"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def tool():
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts/sync-query-concurrency-benchmark.py"
    )
    spec = importlib.util.spec_from_file_location("concurrency_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://127.0.0.1:5432/business",
        "postgresql+psycopg://remote.invalid/sync_redesign_test_perf",
        "postgresql+psycopg://127.0.0.1:5432/sync_redesign_test_perf?host=remote.invalid",
        "postgresql+psycopg://127.0.0.1:5433/sync_redesign_test_perf",
    ],
)
def test_rejects_nonlocal_nonperf_or_connection_override(monkeypatch, url):
    module = tool()
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValueError, match="dedicated local perf"):
        module.guard()


def test_requires_explicit_empty_environment_and_disk_reserve(monkeypatch):
    module = tool()
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://127.0.0.1:5432/sync_redesign_test_perf"
    )
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    with pytest.raises(ValueError, match="APP_ENV_FILE"):
        module.guard()
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setattr(
        module.shutil, "disk_usage", lambda _: SimpleNamespace(free=9 * 1024**3)
    )
    with pytest.raises(ValueError, match="10 GiB"):
        module.guard()
    monkeypatch.setattr(
        module.shutil, "disk_usage", lambda _: SimpleNamespace(free=11 * 1024**3)
    )
    module.guard()


@pytest.mark.parametrize("days", [7, 90])
def test_twenty_cold_cases_are_distinct_and_cover_every_dimension(days):
    module = tool()
    cases = module.distinct_cases(days)
    assert len(cases) == len(set(cases)) == 20
    assert {case.dimension for case in cases} == set(module.DIMENSIONS)
    assert all(1 <= case.days <= days for case in cases)
    assert any(case.days == days and case.dimension == "material" for case in cases)


def test_latency_percentiles_do_not_round_down_the_tail():
    module = tool()
    assert module.distribution(list(range(1, 21))) == {
        "count": 20,
        "p50_ms": 10,
        "p95_ms": 19,
        "max_ms": 20,
    }


def test_explicit_small_validation_database_is_allowed(monkeypatch):
    module = tool()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://127.0.0.1:5432/sync_redesign_test_concurrency",
    )
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setattr(
        module.shutil, "disk_usage", lambda _: SimpleNamespace(free=11 * 1024**3)
    )
    module.guard()


def test_search_oracle_keeps_matching_ads_without_facts():
    from tests.performance.sync_query_fixture import FixtureParameters

    module = tool()
    parameters = FixtureParameters(objects=100, active_fraction=0.1)
    default_total, _ = module.fixture_members(parameters, module.QueryCase("ad", 1))
    search_total, search_page = module.fixture_members(
        parameters, module.QueryCase("ad", 1, query="perf")
    )
    empty_total, empty_page = module.fixture_members(
        parameters, module.QueryCase("ad", 1, include_empty=True)
    )
    assert default_total == 10
    assert search_total == empty_total == 100
    assert search_page == empty_page
