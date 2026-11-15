"""Exclusive-writer real-PG R9 counterexamples. Restore external backup byte-for-byte."""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = "/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py"
BASE = "backend/app/modules/reporting/"
probes = {
    "page-live": (
        "queries.py",
        "items = tuple(frozen_row(row) for row in rows)",
        "items = materialize_rows(session, tenant_id=context.tenant_id, filters=filters, members=list(snapshot_members(session, snapshot=snapshot, rows=rows).values()), advertisers=snapshot.advertiser_ids)",
        "page_survives",
    ),
    "detail-live": (
        "detail.py",
        "report_result = frozen_row(row)",
        "report_result = query_ads(session, context=context, bc_id=bc_id, filters=ReportingFilter.model_validate(snapshot.filters)).items[0]",
        "detail_scope",
    ),
    "evidence": (
        "query_reuse.py",
        "updated_at,active_contract,publication_digest",
        "updated_at,active_contract",
        "evidence_only",
    ),
    "ad-only": ("query_reuse.py", "list(DAY_KINDS)", '["ad_day"]', "evidence_only"),
    "legacy": (
        "query_models.py",
        "if snapshot.result_contract != RESULT_CONTRACT:",
        "if False:",
        "detail_scope",
    ),
    "scope": (
        "queries.py",
        "if snapshot.filter_digest != filter_digest(filters):",
        "if False:",
        "detail_scope",
    ),
    "revoked": (
        "query_models.py",
        "if not set(snapshot.advertiser_ids) <= {grant.advertiser_id for grant in allowed}:",
        "if False:",
        "detail_scope",
    ),
}
for name, (file, old, new, case) in probes.items():
    if sys.argv[1:] and name not in sys.argv[1:]:
        continue
    path = ROOT / BASE / file
    original = path.read_bytes()
    command = [
        "python3",
        RUNNER,
        "python",
        "-m",
        "pytest",
        "backend/tests/modules/reporting/test_native_snapshot.py",
        "-k",
        case,
        "-q",
        "--tb=short",
    ]
    with tempfile.TemporaryDirectory(prefix="jb29-probe-") as tmp:
        backup = Path(tmp) / file
        backup.write_bytes(original)
        try:
            assert old in original.decode(), (name, "target drift")
            path.write_text(original.decode().replace(old, new))
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            assert result.returncode != 0 and (
                "AssertionError" in result.stdout or "DID NOT RAISE" in result.stdout
            ), (name, result.stdout, result.stderr)
            print(name, "business AssertionError RED", flush=True)
        finally:
            path.write_bytes(backup.read_bytes())
            subprocess.run(["cmp", str(path), str(backup)], check=True)
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        print(name, "cmp restored GREEN", flush=True)
