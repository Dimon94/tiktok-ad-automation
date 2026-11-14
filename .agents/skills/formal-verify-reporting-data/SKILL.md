---
name: formal-verify-reporting-data
description: Check ReportingData parent direct authority after changing publication, selected SQL views or derived writers.
---

# ReportingData direct authority

Property: a successful parent direct bucket, including dash and empty, survives every child writer/rebuild. A violation replaces observed platform money with a child's unrelated money.

Run from repository root:

```bash
python3 .agents/skills/formal-verify-reporting-data/scripts/check.py
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python -m pytest backend/tests/modules/reporting/test_native_levels.py -q
```

The runner is local evidence infrastructure, not production configuration. On another host use the project's guarded dedicated test PG/Redis environment. Never use a shared database.

Model: explicit exhaustive finite-state exploration, Python 3 standard library; two parents sharing a remote ID, one child, values absent/40/dash/empty, independent transactions. Direct publication is atomic; derived writers touch only the child projection. Covers R4/R10 selection and physical writer separation only. It does not verify R7 worker/fence races, snapshot R9, SQL permissions, Decimal math, pagination, or the entire M4a. Real PG/Redis tests remain required; the model alone proves no property of the SQL implementation.

Mapping: `publish_parent_day` writes only `parent_report_day`; `publish_ad_day`, projector/rebuild and advertiser restore write the child/derived relation; `campaign_selected_day` and `adgroup_selected_day` suppress derived candidates when direct state has last_success. A complete empty bucket is a direct state with no fact. Model transitions each represent one committed publication or derived update.

Source sync: JB26 candidate based on 7d987d8ebb3bad16b86dbf1e0becd5e7bff04829; see docs/validation/2026-11-14-jb26-native-growth-levels.md for final evidence. Before moving this sync reference, inspect publish.py, levels.py, native_adgroup_day.py, native_campaign_day.py and real writer tests again.

Sensitivity: `--broken-selector` must fail on direct dash + child 999. Run the recorded source probes (validation document) to establish that the PG regressions reject broken real code; restore bytes and rerun green. A model finding must be replayed through real query/publish before being called a product defect. Do not weaken invariants to make a finding disappear.

## JB27 R7 publication extension

Run `python3 .agents/skills/formal-verify-reporting-data/scripts/publication.py` and
`python3 .agents/skills/formal-verify-reporting-data/scripts/source_probes.py`.
The latter requires the guarded MMF local runner above and restores source bytes
in `finally`; run it serially with no other writer or test process.

R7 maps the stable account advisory lock in `_writable_buckets`, equal-time
conflict/no-op in `_publication_buckets`, identity validation in the publishers,
and `TransactionBoundaryReader._check` to a finite component model: two workers,
two ordered observations, three levels, one account/day bucket per level,
VALUE/EMPTY, and monotonic local publication versions. Components factor because
writers are scoped to a single kind; this assumes scope SQL and route checks,
which are tested on real PG rather than proved by the model. Each publication
is one PG transaction (facts, evidence, state and satisfied requirements).
The state space is finite and explicitly explored, not a proof of unbounded
concurrency, SDK correctness, Redis crash recovery or whole-system behavior.

`--broken-fence`, `--broken-watermark`, `--broken-duplicate`, and `--broken-atomic`
must each fail. The source probes bind these failures to real PG regressions;
replay also has an independent source probe. `test_native_publication.py` uses
two concurrent Worker executions, separate sessions and Events, plus real
PG/Redis dispatch. Its Reader boundary tests do not themselves exercise SDK
parsing. `test_native_levels.py` separately traverses refresh HTTP → real SDK
transport → original Worker/checkpoint/publish/query → live workspace, including
quota yield, a failed second page, old readable facts and layer-only retry.

Source sync: JB27 changes built on 7941c17662c2980284491c7fb1548a0638fa07dc;
final commands and limits in docs/validation/2026-11-14-jb27-native-growth-publication.md.
R4/R10's existing 48-state check remains unchanged; R7 is an additional model,
not a retroactive claim about that check.
