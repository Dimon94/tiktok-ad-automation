"""R9 export source counterexamples; exclusive writer, external backup/finally/cmp."""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = "/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py"
FILE = ROOT / "backend/app/modules/reporting/exports.py"
probes = {
    "live": (
        "yield from export.frozen_rows",
        'yield from (r.model_dump(mode="json") for r in __import__("app.modules.reporting.queries", fromlist=["query_ads"]).query_ads(session, context=require_tenant(session, actor_id=export.actor_id, tenant_id=export.tenant_id, action="read"), bc_id=export.bc_id, filters=ReportingFilter.model_validate(export.filters)).items)',
        "copies_observation",
    ),
    "permission": (
        "if not set(export.advertiser_ids) <= allowed_ids:",
        "if False:",
        "scope_revocation",
    ),
    "scope": ("or export.bc_id != bc_id", "or False", "scope_revocation"),
    "legacy": (
        "if export.result_contract != RESULT_CONTRACT:",
        "if False:",
        "legacy_worker",
    ),
    "precision": (
        "value = _decimal(values.get(key))",
        "value = _decimal(float(values[key])) if values.get(key) is not None else None",
        "copies_observation",
    ),
    "null": (
        'data[key] = value if value is not None else ""',
        'data[key] = value if value is not None else "0"',
        "copies_observation",
    ),
}
for name, (old, new, case) in probes.items():
    if sys.argv[1:] and name not in sys.argv[1:]:
        continue
    original = FILE.read_bytes()
    command = [
        "python3",
        RUNNER,
        "python",
        "-m",
        "pytest",
        "backend/tests/modules/reporting/test_native_exports.py",
        "-k",
        case,
        "-q",
        "--tb=short",
    ]
    with tempfile.TemporaryDirectory(prefix="jb30-probe-") as tmp:
        backup = Path(tmp) / FILE.name
        backup.write_bytes(original)
        try:
            assert old in original.decode(), (name, "target drift")
            FILE.write_text(original.decode().replace(old, new))
            result = subprocess.run(
                command, cwd=ROOT, text=True, capture_output=True, check=False
            )
            assert result.returncode != 0 and (
                "AssertionError" in result.stdout or "DID NOT RAISE" in result.stdout
            ), (name, result.stdout, result.stderr)
            print(name, "business AssertionError RED", flush=True)
        finally:
            FILE.write_bytes(backup.read_bytes())
            subprocess.run(["cmp", str(FILE), str(backup)], check=True)
        result = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        print(name, "cmp restored GREEN", flush=True)
