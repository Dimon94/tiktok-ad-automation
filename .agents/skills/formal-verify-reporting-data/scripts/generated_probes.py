"""Serial source probes: assertion RED, external backup/finally/cmp, GREEN.

Logs contain synthetic test failures only, never environment configuration.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = "/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py"
TEST = "backend/tests/modules/reporting/test_native_scope_roas.py"
# Source/contract/subset three unchanged mappings are inherited from scope_probes.
PROBES = (
    (
        "d0-midnight",
        "metric_catalog.py",
        "click_hours=24",
        "click_hours=0",
        "test_generated_field_states_pg",
    ),
    (
        "parent-child-add",
        "row_set.py",
        'f"sum({alias}.{m}) AS {m}, bool_or({alias}.{m} IS NULL) AS {m}_hn"',
        'f"(sum({alias}.{m}) + COALESCE((SELECT sum(child.{m}) FROM campaign_report_day child WHERE child.tenant_id=:tenant AND child.advertiser_id=ANY(:advs) AND child.stat_date BETWEEN :start AND :end),0)) AS {m}, bool_or({alias}.{m} IS NULL) AS {m}_hn"',
        "test_generated_directory_scope_matrix",
    ),
    (
        "average-platform-ratio",
        "dimension_query.py",
        'display[key] = field.get("value")',
        'display[key] = str(sum(Decimal(r["fields"][key]["value"]) for r in records) / len(records)) if len(records) > 1 and all(r["fields"].get(key, {}).get("value") is not None for r in records) else field.get("value")',
        "test_platform_ratio_is_not_averaged_across_days",
    ),
    (
        "cross-currency",
        "row_set.py",
        'currency = identity("t.currency")',
        "currency = \"CASE WHEN t.currency IS NOT NULL THEN 'USD' END\"",
        "test_generated_currency_days_pg",
    ),
    (
        "cross-source-division",
        "row_set.py",
        "return level, source, revision",
        '''return level, "(CASE WHEN t.advertiser_id IS NOT NULL THEN 'PLATFORM_DIRECT' END)", "(CASE WHEN t.advertiser_id IS NOT NULL THEN 'native-growth-day-v1' END)"''',
        "test_source_contract_buckets_are_not_combined",
    ),
    (
        "missing-through-threshold",
        "row_set.py",
        "checks.append(f\"{expr} >= {params.add(f'min_{field}', low)}\")",
        "checks.append(f\"COALESCE({expr},0) >= {params.add(f'min_{field}', low)}\")",
        "test_generated_field_states_pg",
    ),
    (
        "missing-to-zero",
        "dimension_query.py",
        "name: None if name in unsupported else values.get(name)",
        "name: None if name in unsupported else (values.get(name) or Decimal(0))",
        "test_generated_field_states_pg",
    ),
)


def run():
    for name, filename, before, after, node in PROBES:
        if sys.argv[1:] and name not in sys.argv[1:]:
            continue
        path = ROOT / "backend/app/modules/reporting" / filename
        original = path.read_bytes()
        assert original.decode().count(before) == 1, name
        command = [
            "python3",
            RUNNER,
            "python",
            "-m",
            "pytest",
            TEST,
            "-k",
            node,
            "-q",
            "--tb=short",
        ]
        with tempfile.TemporaryDirectory(prefix="jb28-source-") as folder:
            backup = Path(folder) / filename
            backup.write_bytes(original)
            try:
                path.write_text(original.decode().replace(before, after, 1))
                result = subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True, check=False
                )
                (Path("/tmp") / f"jb28-{name}-RED.log").write_text(
                    result.stdout + result.stderr
                )
                assert result.returncode == 1 and "AssertionError" in result.stdout, (
                    result.stdout + result.stderr
                )
                assert (
                    "ERROR collecting" not in result.stdout
                    and "sqlalchemy.exc." not in result.stdout
                )
                print(name + ": business assertion RED", flush=True)
            finally:
                path.write_bytes(backup.read_bytes())
                subprocess.run(["cmp", str(backup), str(path)], check=True)
                assert path.read_bytes() == original
            result = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, check=False
            )
            assert result.returncode == 0, result.stdout + result.stderr
            print(name + ": cmp restored / GREEN", flush=True)


if __name__ == "__main__":
    run()
