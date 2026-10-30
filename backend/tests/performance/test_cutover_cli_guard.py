"""离线迁移配置必须在连接数据库前验证环境、停机及完整迁移范围。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/sync-query-cutover.py"


@pytest.mark.parametrize(
    ("arguments", "explicit_environment", "message"),
    [
        ([], False, "APP_ENV_FILE must explicitly select"),
        (["--apply"], True, "--apply requires --consumers-stopped"),
        (
            ["--background-capacity", "4"],
            True,
            "pool configuration requires --apply --kind all",
        ),
        (
            [
                "--apply",
                "--consumers-stopped",
                "--kind",
                "report",
                "--background-capacity",
                "4",
            ],
            True,
            "pool configuration requires --apply --kind all",
        ),
        (
            ["--apply", "--consumers-stopped", "--background-capacity", "0"],
            True,
            "background capacity must be between",
        ),
        (
            ["--apply", "--consumers-stopped", "--background-prefetch-slots", "-1"],
            True,
            "background prefetch slots must be between",
        ),
    ],
)
def test_cutover_refuses_unsafe_configuration_before_settings(
    tmp_path, arguments, explicit_environment, message
):
    # 不继承任何业务凭据；即使guard回归，也不会连接真实环境。
    environment = {"PATH": os.environ.get("PATH", "")}
    if explicit_environment:
        environment["APP_ENV_FILE"] = "/dev/null"
    output = tmp_path / "cutover.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2, result.stderr
    assert message in result.stderr
    assert "ValidationError" not in result.stderr
    assert not output.exists()
