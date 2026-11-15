"""Serial real-source sensitivity probes; external backups, finally restore + cmp."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[4]
runner = "/tmp/tkada-native-series-20261112/m4b-implementation/run-test.py"
target = root / "backend/app/modules/reporting/material_query.py"
original = target.read_bytes()
probes = {
    "parent": ('"metric_buckets": vectors,', '"metric_buckets": tuple(v.model_copy(update={"values": v.values | {"spend": 100}}) for v in vectors),'),
    "scope": ('"bc_id": snapshot.bc_id,', '"bc_id": "foreign-bc",'),
}
command = ["python3", runner, "python", "-m", "pytest", "backend/tests/modules/reporting/test_material_instance_tracer.py", "-k", "frozen_before", "-q", "--tb=short"]
with tempfile.TemporaryDirectory(prefix="jb34-source-") as directory:
    backup = Path(directory) / target.name
    backup.write_bytes(original)
    probes["tenant-sql"] = ("WHERE f.tenant_id=:tenant AND f.advertiser_id=ANY(:advs)", "WHERE f.advertiser_id=ANY(:advs)")
    probes["freeze"] = ("    items = tuple(frozen_row(row) for row in rows)", """    from app.modules.reporting.material_query import materialize_material_rows, freeze_instance_tracer
    from app.modules.reporting.snapshot_members import snapshot_members
    from app.modules.reporting.creative_query import read_facts
    members = snapshot_members(session, snapshot=snapshot, rows=rows)
    facts = read_facts(session, tenant_id=snapshot.tenant_id, advertisers=snapshot.advertiser_ids, filters=filters, row_keys=[r.row_key for r in rows], observations=True)
    items = freeze_instance_tracer(session, snapshot=snapshot, filters=filters, reports=materialize_material_rows(session, tenant_id=snapshot.tenant_id, advertisers=snapshot.advertiser_ids, filters=filters, members=[members[r.row_key] for r in rows], selected_facts=facts), facts=facts)""")
    for name, (before, after) in probes.items():
        target = root / "backend/app/modules/reporting" / ("creative_query.py" if name == "tenant-sql" else "queries.py" if name == "freeze" else "material_query.py")
        original = target.read_bytes()
        backup = Path(directory) / target.name
        backup.write_bytes(original)
        assert before in original.decode()
        try:
            target.write_text(original.decode().replace(before, after, 1))
            run = subprocess.run(command, cwd=root, capture_output=True, text=True)
            assert run.returncode != 0 and ("AssertionError" in run.stdout or "E   assert" in run.stdout), run.stdout + run.stderr
            print(f"{name}: business AssertionError RED")
        finally:
            target.write_bytes(backup.read_bytes())
            subprocess.run(["cmp", str(backup), str(target)], check=True)
        subprocess.run(command, cwd=root, check=True)
        print(f"{name}: cmp restored; GREEN")
