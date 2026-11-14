"""JB28 source sensitivity, serialized real PG tests with external byte backups."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = "/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py"
TEST = "backend/tests/modules/reporting/test_native_scope_roas.py"
PROBES = (
    (
        "subset-ignored",
        "row_set.py",
        'predicate += " AND t.ad_id=ANY(:contribution_ids)"',
        'predicate += ""',
        "test_explicit_ad_contribution_keeps_cost_five",
    ),
    (
        "source-contract-merged",
        "row_set.py",
        "return level, source, revision",
        '''return level, "(CASE WHEN t.advertiser_id IS NOT NULL THEN 'PLATFORM_DIRECT' END)", "(CASE WHEN t.advertiser_id IS NOT NULL THEN 'native-growth-day-v1' END)"''',
        "test_source_contract_buckets_are_not_combined",
    ),
    (
        "old-contract-interpreted",
        "row_set.py",
        "IN ('{NATIVE_CONTRACT}','LEGACY_UNKNOWN')",
        "IS NOT NULL",
        "test_unknown_direct_contract_is_not_interpreted",
    ),
    (
        "platform-multiday-first",
        "dimension_query.py",
        "if len(daily) == 1 and filters.start_date == filters.end_date",
        "if daily",
        "test_platform_ratio_is_not_averaged_across_days",
    ),
)

for name, filename, before, after, node in PROBES:
    path = ROOT / "backend/app/modules/reporting" / filename
    original = path.read_bytes()
    assert original.decode().count(before) == 1, name
    command = [
        "python3",
        RUNNER,
        "python",
        "-m",
        "pytest",
        f"{TEST}::{node}",
        "-q",
        "--tb=short",
    ]
    with tempfile.TemporaryDirectory(prefix="jb28-scope-probe-") as directory:
        backup = Path(directory) / filename
        backup.write_bytes(original)
        try:
            path.write_text(original.decode().replace(before, after, 1))
            result = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, check=False
            )
            assert result.returncode == 1 and "AssertionError" in result.stdout, (
                result.stdout + result.stderr
            )
            print(name + ": concrete assertion RED", flush=True)
        finally:
            path.write_bytes(backup.read_bytes())
            assert path.read_bytes() == original, name
        subprocess.run(command, cwd=ROOT, check=True)
        print(name + ": bytes restored / GREEN", flush=True)
