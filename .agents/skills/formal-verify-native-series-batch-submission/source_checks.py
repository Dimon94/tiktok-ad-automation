# ruff: noqa: T201
"""Serial scratch mutations. Backups/logs outside repo; restore bytes even on failure.
Run only with exclusive source ownership and isolated PostgreSQL/Redis test runner.
"""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BACKEND = ROOT / "backend"
parser = argparse.ArgumentParser()
parser.add_argument("--runner", required=True, help="Existing isolated M2 run-test.py")
parser.add_argument("--out", required=True, help="External evidence directory")
parser.add_argument("--only", default="all")
args = parser.parse_args()
out = Path(args.out).resolve()
assert not out.is_relative_to(ROOT)
out.mkdir(parents=True, exist_ok=True)
B = "tests/modules/builds/"
cases = [
    (
        "budget-double",
        "app/modules/builds/frozen_requests.py",
        'Decimal(p["campaign"]["budget"])',
        '2 * Decimal(p["campaign"]["budget"])',
        [B + "test_native_formal_inputs.py"],
    ),
    (
        "budget-float",
        "app/modules/builds/frozen_requests.py",
        'Decimal(p["campaign"]["budget"])',
        'Decimal(float(p["campaign"]["budget"]))',
        [B + "test_native_formal_inputs.py"],
    ),
    (
        "digest",
        "app/modules/builds/frozen_requests.py",
        "payload, sort_keys=True, separators=",
        '(payload if "plans" not in payload else {}), sort_keys=True, separators=',
        [B + "test_native_batch_preview.py::test_manifest_digest_binds_every_field"],
    ),
    (
        "completion",
        "app/modules/builds/native_completion.py",
        "def unit_native_matched(session, submission, unit):\n",
        "def unit_native_matched(session, submission, unit):\n    return True\n",
        [B + "test_native_batch_execution.py", "-k", "submit_and_execute and zero"],
    ),
    (
        "unit-scope",
        "app/modules/builds/native_execution.py",
        "    plan_key = None\n",
        "    unit_id = units[0].id\n    plan_key = None\n",
        [B + "test_native_batch_execution.py::test_validate_current_plan"],
    ),
    (
        "actor-stamp",
        "app/modules/tenants/permissions.py",
        "if submission.authorization_stamp != current:",
        "if False:",
        [
            B + "test_native_batch_execution.py",
            "-k",
            "fences and user_regrant and send",
        ],
    ),
    (
        "receipt",
        "app/modules/builds/submissions.py",
        "status=get_submission(\n                session, context=context, submission_id=saved.id\n            ).status,",
        'status="COMPLETED",',
        [B + "test_native_batch_execution.py", "-k", "submit_and_execute and unknown"],
    ),
    (
        "account-lock",
        "app/modules/builds/native_execution.py",
        "    for model, key in rows:\n",
        "    rows = tuple((m, k) for m, k in rows if m is not BCAccountAccess)\n    for model, key in rows:\n",
        [B + "test_native_batch_execution.py", "-k", "send-lock"],
    ),
]


def check(name, targets, expected):
    result = subprocess.run(
        [
            "python3",
            args.runner,
            "python",
            "-m",
            "pytest",
            *targets,
            "-q",
            "--tb=short",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    log = result.stdout + result.stderr
    (out / f"{name}.log").write_text(log)
    assert result.returncode == expected, f"{name}: exit {result.returncode}; see {out}"
    if expected:
        assert "AssertionError" in log or "assert " in log, (
            f"{name}: not an assertion failure"
        )
    print(name, "RED" if expected else "GREEN", flush=True)


for name, filename, old, new, targets in cases:
    if args.only not in ("all", name):
        continue
    p = BACKEND / filename
    original = p.read_bytes()
    assert original.decode().count(old) == 1, f"Review changed owner: {filename}"
    with tempfile.TemporaryDirectory(prefix="jb16-source-backup-") as tmp:
        backup = Path(tmp) / p.name
        backup.write_bytes(original)
        try:
            p.write_text(original.decode().replace(old, new, 1))
            check(name + "-broken", targets, 1)
        finally:
            p.write_bytes(backup.read_bytes())
            assert p.read_bytes() == original
            subprocess.run(["cmp", str(p), str(backup)], check=True)
    check(name + "-restored", targets, 0)
