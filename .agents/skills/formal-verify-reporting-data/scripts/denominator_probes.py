"""Real PG source mutation; external byte backup and unconditional restoration."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = "/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py"
TEST = "backend/tests/modules/reporting/test_native_scope_roas.py"
PROBES = (
    (
        "python-denominator",
        "dimension_query.py",
        "and denominator not in missing_denominators",
        "and True",
    ),
    (
        "sql-denominator",
        "row_set.py",
        "WHEN NOT {alias}.{denominator}_hn THEN",
        "WHEN TRUE THEN",
    ),
    (
        "account-derived",
        "row_set.py",
        '"account": ("campaign_selected_day", None)',
        '"account": ("campaign_report_day", None)',
    ),
)


def run():
    return subprocess.run(
        ["python3", RUNNER, "python", "-m", "pytest", TEST, "-q", "--tb=short"],
        cwd=ROOT,
        check=False,
    ).returncode


for name, filename, before, after in PROBES:
    path = ROOT / "backend/app/modules/reporting" / filename
    original = path.read_bytes()
    assert before in original.decode(), name
    with tempfile.TemporaryDirectory(prefix="jb28-probe-") as directory:
        backup = Path(directory) / filename
        backup.write_bytes(original)
        try:
            path.write_text(original.decode().replace(before, after, 1))
            assert run() != 0, f"undetected mutation: {name}"
        finally:
            path.write_bytes(backup.read_bytes())
            assert path.read_bytes() == backup.read_bytes(), name
            assert run() == 0, f"failed after restoration: {name}"
    print(f"{name}: RED / bytes restored / GREEN", flush=True)
