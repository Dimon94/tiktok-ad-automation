"""回填命令必须在读取部署配置前要求显式环境。"""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

BACKEND = Path(__file__).resolve().parents[2]


def run_cli(*args):
    # 即使guard回归，也不能读工作树../.env或继承业务连接/凭据。
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(BACKEND)}
    with TemporaryDirectory() as directory:
        cwd = Path(directory) / "work"
        cwd.mkdir()
        return subprocess.run(
            [sys.executable, "-m", "app.modules.reporting.read_models.rebuild", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )


def test_rebuild_help_does_not_load_application_settings():
    result = run_cli("--help")
    assert result.returncode == 0, result.stderr
    assert "--checkpoint" in result.stdout


def test_rebuild_requires_explicit_environment_before_loading_settings(tmp_path):
    result = run_cli(
        "--tenant",
        "00000000-0000-0000-0000-000000000001",
        "--advertiser",
        "guard-only",
        "--start",
        "2026-01-01",
        "--end",
        "2026-01-01",
        "--checkpoint",
        str(tmp_path / "checkpoint.json"),
    )
    assert result.returncode == 2
    assert "APP_ENV_FILE must explicitly select" in result.stderr
    assert "ValidationError" not in result.stderr
    assert not (tmp_path / "checkpoint.json").exists()
