---
name: formal-verify-reporting-data
description: Verify ReportingData direct authority, publication and bounded Native Growth scope/Decimal contracts.
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

Historical source sync (superseded by JB28 sync_commit below): JB26 candidate based on 7d987d8ebb3bad16b86dbf1e0becd5e7bff04829; see docs/validation/2026-11-14-jb26-native-growth-levels.md for final evidence. Before moving this sync reference, inspect publish.py, levels.py, native_adgroup_day.py, native_campaign_day.py and real writer tests again.

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

Historical source sync (superseded by JB28 sync_commit below): JB27 changes built on 7941c17662c2980284491c7fb1548a0638fa07dc;
final commands and limits in docs/validation/2026-11-14-jb27-native-growth-publication.md.
R4/R10's existing 48-state check remains unchanged; R7 is an additional model,
not a retroactive claim about that check.

## JB28 R2/R5/R6/R8 finite generated verification

sync_commit: `a749ad484ed76d48d41bde2c588eb62706996669` (product head personally checked).
R4/R10 and R7 descriptions remain in sync at this head: direct publication and
watermark transaction boundaries are unchanged; empty publication coordinates
are now part of the same atomic payload digest. See the drift audit and exact
inherited/current evidence in
[JB28 formal validation](../../../docs/validation/2026-11-14-jb28-native-growth-formal.md).

Run these from the product repository root, serially:

```bash
python3 .agents/skills/formal-verify-reporting-data/scripts/check.py
python3 .agents/skills/formal-verify-reporting-data/scripts/publication.py
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python .agents/skills/formal-verify-reporting-data/scripts/generated.py
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py -k 'not representative' -q --tb=short
```

Generated exploration checks 1,296 two-member money cases with independent
Fraction expectations: NULL/DASH/ABSENT/zero/0.1/90 per spend and revenue.
Nine one-coordinate partitions cover level/source/contract/currency;
12 local-clock cases cover UTC/Hong_Kong/Los_Angeles including midnight.
Maps to metric_vector/aggregate_metrics/merge_metric_buckets and
metric_catalog.semantics_for. Arithmetic abstraction merges three missing
states; real PG tests check their distinct field_state_counts and SQL threshold,
NULL-last, summaries, complete denominator counts, and MetricVector values.

Scope is tested through the actual ReportingFilter/AdContributionScope path:
three levels and supported parent contribution masks, directory attributes,
parent paths, source buckets and cross-currency local days. No arbitrary window
or creative/material parent contribution input is invented. Native revision
fixes each metric window; D0 is click24h, total cutoff stays unknown.

Seven bounded property families and their PG bindings are enumerated in the
validation document. Assumes at most two arithmetic members, fixed metric
contracts, committed observations and key equality independent across axes.
This is not an unbounded SQL, platform attribution, event maturity, liveness,
DST-transition, permission or snapshot proof. R9/JB29 is outside this check.
The six-case denominator.py remains historical example evidence only.

Sensitivity (exclusive writer; never overlap fixture suites):

```bash
python3 .agents/skills/formal-verify-reporting-data/scripts/generated_probes.py
python3 .agents/skills/formal-verify-reporting-data/scripts/scope_probes.py
```

Both scripts support optional probe names to run a bounded subset. Each source
probe requires business AssertionError RED, restores an external backup in
finally, checks bytes (generated_probes also invokes cmp), then requires GREEN.
Generated probes cover parent-child addition, average platform ratio,
cross-currency, cross-source division, missing-to-zero/threshold and D0 window.
Old scope probe's daily→records mapping was updated without weakening the
property. Existing denominator/source/empty-coordinate probe evidence can be
inherited only when the actual mutation target and regression still match.
Report surviving mutations as reachability/test gaps first; replay any genuine
counterexample against real PG and report the product owner, never weaken the
property or change product code here.

## JB29 R9 frozen query observations

sync_commit: `8f986018f3dc389aeebaf8002a82e86e1d1ff947` (product candidate checked).
Outcome: **updated** for R9; R4/R10 and R7 remain **in sync**. Their publishers,
level registry and physical selected-day authority views did not change from
47359643. JB28 vector arithmetic and metric definitions are unchanged; selected
observations now reach the same formatter from the SQL snapshot projection.
Actual source/field-state/scope/denominator expectations were rerun through that
path. Read [JB29 evidence](../../../docs/validation/2026-11-14-jb29-native-growth-snapshot.md)
for the AC matrix, actual failures/fixes and inherited boundaries.

Run from the product root, serially in the guarded dedicated environment:

```bash
python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot.py
python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot.py --broken-read
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python -m pytest backend/tests/modules/reporting/test_native_snapshot.py -q
python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot_probes.py
```

The normal model must pass; `--broken-read` must exit with AssertionError.
Source probes require an exclusive writer and no concurrent fixture suite. They
accept business value assertions or an expected HTTP rejection not raised;
SQL/import failures are not valid RED. They restore a repository-external backup
in finally, invoke cmp and require the same regression GREEN. Optional arguments
select names: page-live, detail-live, evidence, ad-only, legacy, scope, revoked.

R9's finite abstraction enumerates 128 combinations: three independent binary
layer publication coordinates before/after a query, plus an evidence-only change.
Both page and detail must equal the captured query observation after publication.
Each publication coordinate denotes one committed observation, not a common
platform timestamp. Maps to row_set.insert_snapshot_rows, RR _build_snapshot /
freeze_results, _read_page / frozen_row and get_ad_detail. New queries can select
the latest coordinates without rewriting the previous result. The model assumes
atomic committed publication and an immutable captured tuple; actual atomicity,
SQL selection, current permissions, expiry and contract compatibility are bound
by PG tests/probes rather than proved by this arithmetic abstraction.

The real two-connection PG race commits a new publication between SQL snapshot
INSERT and formatting: old row/summary stay 10/20, next query sees 100/200. The
source probes additionally reject live page/detail recomputation, missing digest,
Ad-only dependency, legacy reinterpretation, scope drift and revoked access.
No new product defect is left open from these checks; the empty-Account level
and layer-overview regressions found during verification were product bugs and
were fixed in the candidate. This does not prove unbounded concurrency, liveness,
SDK behavior, arbitrary production scale or CSV consistency; CSV remains JB30.
