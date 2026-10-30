"""真实COMMIT压测工具在连接前拒绝非隔离配置。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def tool():
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts/sync-directory-commit-benchmark.py"
    )
    spec = importlib.util.spec_from_file_location("directory_commit_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://localhost/business",
        "postgresql+psycopg://remote.invalid/sync_redesign_test_publish",
        "postgresql+psycopg://127.0.0.1/sync_redesign_test_publish?host=remote.invalid",
        "postgresql+psycopg://127.0.0.1/sync_redesign_test_publish#override",
    ],
)
def test_directory_commit_benchmark_rejects_nonlocal_database(monkeypatch, url):
    benchmark = tool()
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValueError, match="dedicated local publish"):
        benchmark.guard()


def test_directory_commit_benchmark_requires_environment_and_disk(monkeypatch):
    benchmark = tool()
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://127.0.0.1/sync_redesign_test_publish"
    )
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    with pytest.raises(ValueError, match="APP_ENV_FILE"):
        benchmark.guard()
    monkeypatch.setenv("APP_ENV_FILE", "/dev/null")
    monkeypatch.setattr(
        benchmark.shutil, "disk_usage", lambda _: SimpleNamespace(free=9 * 1024**3)
    )
    with pytest.raises(ValueError, match="10 GiB"):
        benchmark.guard()
    monkeypatch.setattr(
        benchmark.shutil, "disk_usage", lambda _: SimpleNamespace(free=11 * 1024**3)
    )
    benchmark.guard()
