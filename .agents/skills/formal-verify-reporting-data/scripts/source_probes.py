"""Run destructive source probes serially, restore exact bytes in finally, rerun green."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[4]
runner = '/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py'
publish = root / 'backend/app/modules/reporting/publish.py'
executions = root / 'backend/app/modules/sync_control/executions.py'
base = 'backend/tests/modules/reporting/test_native_publication.py'
probes = [
    ('watermark', publish, 'and watermark > requested_at', 'and False', base + '::test_same_observation_replay_noop_and_conflict'),
    ('replay', publish, '    writable = set(buckets)', '    return set(buckets)\n    writable = set(buckets)', base + '::test_same_observation_replay_noop_and_conflict'),
    ('duplicate', publish, 'or (adv, rid, day) in seen', 'or False', base + '::test_single_page_duplicate_is_domain_error'),
    ('fence', executions, '            current is None\n', '            False and (current is None\n', base + '::test_two_workers_reject_lost_fence'),
    ('atomic', publish, '    session.add_all([r for r in staged if (r.advertiser_id, r.stat_date) in buckets])', '    session.add_all([r for r in staged if (r.advertiser_id, r.stat_date) in buckets])\n    session.commit()', 'backend/tests/modules/reporting/test_native_levels.py::test_adgroup_publication_rolls_back_fact_evidence_and_state'),
]
for name, path, old, new, test in probes:
    if len(sys.argv) > 1 and name != sys.argv[1]: continue
    original = path.read_bytes()
    source = original.decode()
    assert old in source
    modified = source.replace(old, new, 1)
    if name == 'fence':
        modified = modified.replace('            or current.claimed_until <= datetime.now(UTC)\n', '            or current.claimed_until <= datetime.now(UTC))\n', 1)
    command = [sys.executable, runner, 'python', '-m', 'pytest', test, '-q', '--tb=short']
    try:
        path.write_text(modified)
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        assert result.returncode != 0, (name, 'probe missed')
        print(name, 'RED', result.stdout.splitlines()[-1:])
    finally:
        path.write_bytes(original)
        assert path.read_bytes() == original
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    print(name, 'restored', result.stdout.splitlines()[-1:])
    assert result.returncode == 0, result.stdout[-3000:]
