"""R1 precise-scope regression binds bounded coordinates to real PG selection."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
path = ROOT / "backend/app/modules/reporting/row_set.py"
original = path.read_bytes()
command = [
    "python3",
    "/tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py",
    "python",
    "-m",
    "pytest",
    "backend/tests/modules/reporting/test_targeted_provenance.py",
    "-k",
    "targeted_observation",
    "-xq",
    "--tb=short",
]
probes = {
    "old-account-observation": (
        "publication = \"CASE WHEN row_evidence.advertiser_id IS NOT NULL THEN row_evidence.evidence->'publication' ELSE to_jsonb(s) END\"",
        'publication = "to_jsonb(s)"',
    ),
    "borrowed-contract": (
        'revision = f"CASE WHEN row_evidence.advertiser_id IS NOT NULL THEN CASE',
        'revision = f"CASE WHEN FALSE THEN CASE',
    ),
}
with tempfile.TemporaryDirectory(prefix="tkada-r1-targeted-") as tmp:
    backup = Path(tmp) / "row_set.py"
    backup.write_bytes(original)
    for name, (old, new) in probes.items():
        assert original.decode().count(old) == 1, name
        try:
            path.write_text(original.decode().replace(old, new, 1))
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            assert result.returncode and "AssertionError" in result.stdout, (
                result.stdout + result.stderr
            )
            print(name, "business RED")
        finally:
            path.write_bytes(backup.read_bytes())
            subprocess.run(["cmp", str(backup), str(path)], check=True)
        subprocess.run(command, cwd=ROOT, check=True)
        print(name, "restored cmp GREEN")
