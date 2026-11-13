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
        "    from .native_preview import read_batch\n",
        "    unit_id = units[0].id  # broken: bypass frozen Unit authorization\n    from .native_preview import read_batch\n",
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


# 组合 owner 的错误必须一并破坏冗余保护；不修改数据库约束或发送事实。
compound_cases = [
    (
        "recovery-ambiguous",
        [
            (
                "app/modules/builds/reconciliation_scan.py",
                'next_progress["matches"] != 1:',
                'next_progress["matches"] < 1:',
            ),
        ],
        [B + "test_native_batch_execution.py::test_batch_submit_and_execute[recovery]"],
        "AssertionError: R10 candidate many",
    ),
    (
        "unique-replay",
        [
            (
                "app/modules/builds/submissions.py",
                "    if existing is None:\n",
                "    existing = None  # broken: forget preview identity on a new key\n    if existing is None:\n",
            )
        ],
        [
            B
            + "test_native_batch_execution.py::test_same_preview_replay_returns_original"
        ],
        "R8 replay must return original Submission",
    ),
    (
        "unknown-retry",
        [
            (
                "app/modules/builds/execution.py",
                'or step.status in {"SUCCEEDED", "FAILED", "UNKNOWN"}\n            or step.remote_id\n            or step.phase == "REQUEST_ARMED"',
                'or step.status in {"SUCCEEDED", "FAILED"}\n            or step.remote_id\n            or (step.phase == "REQUEST_ARMED" and step.status != "UNKNOWN")',
            ),
            (
                "app/modules/builds/submissions.py",
                'step.status in {"SUCCEEDED", "FAILED", "UNKNOWN"}\n        or step.remote_id\n        or step.phase == "REQUEST_ARMED"',
                'step.status in {"SUCCEEDED", "FAILED"}\n        or step.remote_id\n        or (step.phase == "REQUEST_ARMED" and step.status != "UNKNOWN")',
            ),
            (
                "app/modules/builds/submissions.py",
                "or (step.lease_expires_at is not None and step.lease_expires_at > now)",
                'or (step.status != "UNKNOWN" and step.lease_expires_at is not None and step.lease_expires_at > now)',
            ),
            (
                "app/modules/builds/submissions.py",
                "elif not safely_retryable_attempt(session, step=step):",
                'elif step.status != "UNKNOWN" and not safely_retryable_attempt(session, step=step):',
            ),
        ],
        [B + "test_native_batch_execution.py", "-k", "submit_and_execute and unknown"],
        "R9 duplicate worker added transport write",
    ),
    (
        "cancel-send-fence",
        [
            (
                "app/modules/builds/execution_state.py",
                '    if row.progress.get("local_cancelled"):\n',
                '    if False:  # broken: arm and physical send ignore committed cancellation\n',
            )
        ],
        [
            B
            + "test_native_batch_execution.py::test_cancel_before_final_send_fence"
        ],
        "assert outcome ==",
    ),
]


def check(name, targets, expected, failure=None):
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
        check=False,
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
    if expected and failure:
        assert failure in log, f"{name}: wrong behavioral failure; see {out}"
    print(name, "RED" if expected else "GREEN", flush=True)


all_cases = [
    (name, [(filename, old, new)], targets, None)
    for name, filename, old, new, targets in cases
] + compound_cases
assert args.only == "all" or args.only in {c[0] for c in all_cases}
for name, edits, targets, failure in all_cases:
    if args.only not in ("all", name):
        continue
    originals = {
        BACKEND / filename: (BACKEND / filename).read_bytes()
        for filename, _, _ in edits
    }
    changed = {p: data.decode() for p, data in originals.items()}
    for filename, old, new in edits:
        p = BACKEND / filename
        assert changed[p].count(old) == 1, f"Review changed owner: {filename}"
        changed[p] = changed[p].replace(old, new, 1)
    with tempfile.TemporaryDirectory(prefix="jb16-source-backup-") as tmp:
        backups = {p: Path(tmp) / p.name for p in originals}
        for p, data in originals.items():
            backups[p].write_bytes(data)
        try:
            for p, data in changed.items():
                p.write_text(data)
            check(name + "-broken", targets, 1, failure)
        finally:
            for p, backup in backups.items():
                p.write_bytes(backup.read_bytes())
            for p, backup in backups.items():
                assert p.read_bytes() == originals[p]
                subprocess.run(["cmp", str(p), str(backup)], check=True)
    check(name + "-restored", targets, 0)
