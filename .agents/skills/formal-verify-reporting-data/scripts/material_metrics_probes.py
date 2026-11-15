"""Business probes against actual owners, serial external backup/finally/cmp."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[4]
runner = '/tmp/tkada-native-series-20261112/m4b-implementation/run-test.py'
command = ['python3', runner, 'python', '-m', 'pytest', 'backend/tests/modules/reporting/test_material_metrics.py', '-q', '--tb=short']
probes = [
    ('parent-scope', 'material_query.py', '"metric_buckets": parent_row.metric_buckets,', '"metric_buckets": vectors,'),
    ('cross-scope', 'material_query.py', 'if not facts or {fact_key(f) for f in facts} - selected_keys:', 'if not facts:'),
    ('nullzero', 'material_query.py', '            vectors = merge_metric_buckets(', '            items = [dict(f) | {"revenue_total": f["revenue_total"] or 0} for f in items]\n            vectors = merge_metric_buckets('),
    ('parent-total', 'frozen_result.py', 'for i in report.material_instances\n                if i.row_kind == "INSTANCE"', 'for i in (*report.material_instances, *report.parent_controls)'),
]
with tempfile.TemporaryDirectory(prefix='jb36-source-') as directory:
    for name, filename, before, after in probes:
        target = root / 'backend/app/modules/reporting' / filename
        original = target.read_bytes()
        backup = Path(directory) / filename
        backup.write_bytes(original)
        assert before in original.decode(), name
        try:
            target.write_text(original.decode().replace(before, after, 1))
            run = subprocess.run(command, cwd=root, capture_output=True, text=True)
            assert run.returncode and ('AssertionError' in run.stdout or 'E   assert' in run.stdout), run.stdout + run.stderr
            print(name + ': business RED')
        finally:
            target.write_bytes(backup.read_bytes())
            subprocess.run(['cmp', str(target), str(backup)], check=True)
        subprocess.run(command, cwd=root, check=True)
        print(name + ': cmp restored; GREEN')
