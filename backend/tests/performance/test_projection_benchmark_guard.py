"""容量工具在导入应用/连接数据库之前拒绝非隔离环境。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def tool():
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts/sync-query-projection-benchmark.py"
    )
    spec = importlib.util.spec_from_file_location("projection_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://localhost/business",
        "postgresql+psycopg://remote.invalid/sync_redesign_test_perf",
        "postgresql+psycopg://127.0.0.1:5432/sync_redesign_test_perf?host=remote.invalid",
    ],
)
def test_projection_benchmark_rejects_nonlocal_or_nonperf_database(monkeypatch, url):
    benchmark = tool()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    with pytest.raises(ValueError, match="dedicated local perf"):
        benchmark.guard()


def test_projection_benchmark_requires_explicit_empty_environment(monkeypatch):
    benchmark = tool()
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://127.0.0.1:5432/sync_redesign_test_perf"
    )
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    with pytest.raises(ValueError, match="APP_ENV_FILE"):
        benchmark.guard()
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setattr(
        benchmark.shutil, "disk_usage", lambda _: SimpleNamespace(free=11 * 1024**3)
    )
    benchmark.guard()
