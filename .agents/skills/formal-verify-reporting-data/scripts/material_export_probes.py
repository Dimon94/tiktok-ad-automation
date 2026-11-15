"""Exclusive writer only: actual source RED -> external backup/finally/cmp -> GREEN."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[4]
runner = '/tmp/tkada-native-series-20261112/m4b-implementation/run-test.py'
command = ['python3', runner, 'python', '-m', 'pytest', 'backend/tests/modules/reporting/test_material_export_lifecycle.py', '-q', '--tb=short']
probes = [
    ('retry', 'exports.py', '        dispatch.published_at = None', '        pass  # injected lost retry'),
    ('partial', 'exports.py', 'def _delete_object(key: str) -> None:', 'def _delete_object(key: str) -> None:\n    return  # injected orphaned object'),
    ('scope', 'exports.py', '        export.tenant_id != context.tenant_id\n        or export.bc_id != bc_id\n        or export.actor_id != context.actor_id', '        False'),
    ('expiry', 'exports.py', '    if export.expires_at <= datetime.now(UTC):', '    if False and export.expires_at <= datetime.now(UTC):'),
    ('live', 'exports.py', '    seen_parents = set()', '''    from sqlalchemy import text
    current = _session.execute(text("SELECT spend FROM creative_report_day WHERE tenant_id=:t AND advertiser_id='acc-1'"), {"t": export.tenant_id}).scalar()
    for row in export.frozen_rows:
        for instance in row.get("material_instances", []):
            for bucket in instance["metric_buckets"]:
                bucket["values"]["spend"] = str(current)
    seen_parents = set()'''),
]
with tempfile.TemporaryDirectory(prefix='jb38-probe-') as directory:
    for name, filename, before, after in probes:
        target = root / 'backend/app/modules/reporting' / filename
        backup = Path(directory) / filename
        backup.write_bytes(target.read_bytes())
        assert before in target.read_text(), name
        try:
            target.write_text(target.read_text().replace(before, after))
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            assert result.returncode and ('AssertionError' in result.stdout or 'E   assert' in result.stdout or 'DID NOT RAISE' in result.stdout), result.stdout + result.stderr
            print(name + ': business RED', flush=True)
        finally:
            target.write_bytes(backup.read_bytes())
            subprocess.run(['cmp', str(target), str(backup)], check=True)
        subprocess.run(command, cwd=root, check=True)
        print(name + ': restored/cmp GREEN', flush=True)
