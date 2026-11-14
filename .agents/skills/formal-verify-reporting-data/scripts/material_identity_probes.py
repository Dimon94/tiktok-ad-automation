"""JB35 real PG source sensitivity, exclusive writer only."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[4]
target = root / "backend/app/modules/reporting/material_query.py"
command = ["python3", "/tmp/tkada-native-series-20261112/m4b-implementation/run-test.py", "python", "-m", "pytest", "backend/tests/modules/reporting/test_material_instance_tracer.py", "-k", "multi_creative", "-q", "--tb=short"]
original = target.read_bytes()
probes = {
    "double-count": ('            vectors = merge_metric_buckets(', '            items = items * 2\n            vectors = merge_metric_buckets('),
    "null-to-zero": ('            vectors = merge_metric_buckets(', '            items = [dict(f) | {"spend": f["spend"] if f["spend"] is not None else 0} for f in items]\n            vectors = merge_metric_buckets('),
}
with tempfile.TemporaryDirectory(prefix="jb35-probes-") as directory:
    backup = Path(directory) / target.name
    backup.write_bytes(original)
    for name, (before, after) in probes.items():
        assert before in original.decode()
        try:
            target.write_text(original.decode().replace(before, after, 1))
            run = subprocess.run(command, cwd=root, capture_output=True, text=True)
            assert run.returncode and ("AssertionError" in run.stdout or "E   assert" in run.stdout), run.stdout + run.stderr
            print(name + ": business RED")
        finally:
            target.write_bytes(backup.read_bytes())
            subprocess.run(["cmp", str(target), str(backup)], check=True)
        subprocess.run(command, cwd=root, check=True)
        print(name + ": cmp restored; GREEN")
