"""Actual-source business rejection with external backup, finally restore and cmp."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[4]
runner = '/tmp/tkada-native-series-20261112/m4b-implementation/run-test.py'
command = ['python3', runner, 'python', '-m', 'pytest', 'backend/tests/modules/reporting/test_material_query_lifecycle.py', '-q', '--tb=short']
probes = [
    ('reuse-old-contract', 'query_reuse.py', '    if snapshot and snapshot_contract_current(snapshot):', '    if snapshot:'),
    ('partial-commit', 'frozen_result.py', '        after = rows[-1].stable_sequence', '        session.connection().commit()\n        after = rows[-1].stable_sequence'),
    ('expired-access', 'query_models.py', '    if snapshot.expires_at <= datetime.now(UTC):', '    if False and snapshot.expires_at <= datetime.now(UTC):'),
    ('silent-failure', 'page_cache.py', '        _publish_failure(redis, key, token)', '        pass  # injected lost failure notification'),
]
with tempfile.TemporaryDirectory(prefix='jb37-source-') as directory:
    for name, filename, before, after in probes:
        target = root / 'backend/app/modules/reporting' / filename
        original = target.read_bytes()
        backup = Path(directory) / filename
        backup.write_bytes(original)
        assert before in original.decode(), name
        try:
            target.write_text(original.decode().replace(before, after))
            run = subprocess.run(command, cwd=root, capture_output=True, text=True)
            assert run.returncode and ('AssertionError' in run.stdout or 'E   assert' in run.stdout), run.stdout + run.stderr
            print(name + ': business RED', flush=True)
        finally:
            target.write_bytes(backup.read_bytes())
            subprocess.run(['cmp', str(target), str(backup)], check=True)
        subprocess.run(command, cwd=root, check=True)
        print(name + ': cmp restored; GREEN', flush=True)
