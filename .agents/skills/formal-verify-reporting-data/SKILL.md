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

## JB30 R9 independent export lifetime

sync_commit: `2a42c983e6dd364032f6ea7875b29d2be1080a90` (product head checked).
Outcome **updated** for export R9; R4/R10, R7 and JB28 arithmetic remain **in sync**.
The covered publisher/selector/query/vector/catalog code has no diff from JB29's
8f986018 source sync. Query model additions only extend the existing export
record; no new publication or arithmetic owner. The old 128-case description
still passes but did not describe export consumption. `snapshot.py` now includes
an export-owned captured tuple surviving removal of the source snapshot. Normal
passes; `--broken-export` and `--broken-read` each fail on evidence-only refresh.
This keeps the original finite bounds/atomicity and immutable-capture assumptions;
it does not prove file storage, permissions, arbitrary concurrency or capacity.

Run serially from the product root using the dedicated local guard:

```bash
python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot.py
python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot.py --broken-export
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python -m pytest backend/tests/modules/reporting/test_native_exports.py -q
python3 .agents/skills/formal-verify-reporting-data/scripts/export_probes.py
```

Broken model command must exit with AssertionError. Six source mutations map the
new property to real PG: export live recomputation, removed current grants,
removed BC check, legacy task reinterpretation, float money and NULL→zero. Each
must fail on a business assertion, restore external backup in finally with cmp,
and pass unchanged GREEN. The probes accept optional names `live permission
scope legacy precision null`. Do not run any fixture suite concurrently.
Existing snapshot scope/page/detail probes and R7 at-most-once-publication
mapping remain source-matching inherited evidence; export duplicate creation
uses the original idempotency owner rather than a second publication mechanism.

The export PG regression publishes independent 0.100000000000000001/0/0.3/dash
values, creates the task, refreshes source, deletes the original snapshot, and
requires original CSV/vector/map plus cleanup of copied payload. Separate
regressions bind current access, repeated creation/retry and legacy Worker
failure/preserved completed bytes. The original live native_levels harness now
adds real Uvicorn create/download, actual Celery on a unique Redis queue, three
CSV comparisons and HTTP revocation after SDK source refresh. No business
service or browser response double is introduced.

Optional, deliberately creates and retains a new dedicated loopback test DB:

```bash
python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py python .agents/skills/formal-verify-reporting-data/scripts/upgrade_rehearsal.py
```

It loads exact old application git blobs in memory (no checkout/branch creation),
commits through the real old writer then exits that process; upgrades via
Alembic, verifies unchanged old money/LEGACY_UNKNOWN/no parent backfill and
rollback refusal, then runs the same-version SDK/API/Worker/export seam. Never
point it at production. See [JB30 validation](../../../docs/validation/2026-11-14-jb30-native-growth-csv.md)
for actual commands/failures/measurements and inherited T/R matrix. No model
finding is called a product defect without the corresponding real code failure.

## M4a R1 precise Ad observations

sync_commit: `95c5f2728b626522c9228a5090ba3393a380fa5c` (product source checked).
Outcome **updated** for the R9 observation abstraction; R4/R10 direct authority
and whole-account R7 publication remain **in sync**. Read the diff from JB30
`2a42c983e6dd364032f6ea7875b29d2be1080a90` through this product commit,
particularly publish.py, row_set.py, dimension_query.py and frozen_result.py.
The old 128-state snapshot model still passes but its single coordinate per
layer cannot describe targeted facts mixed with older untouched Ad facts.
It is supplemented, not used as proof of the new selection.

```bash
python3 .agents/skills/formal-verify-reporting-data/scripts/targeted.py
python3 .agents/skills/formal-verify-reporting-data/scripts/targeted.py --broken-account-stamp
python3 .agents/skills/formal-verify-reporting-data/scripts/targeted_probes.py
```

Normal targeted model passes 8 cases (existing/no account publication, two
abstract payload digests, prior native/legacy contract). Broken model must fail.
Each fact is one paired abstract money token and immutable observation. A target
transition replaces only C1; account t0/V/H and C2 remain untouched. The derived
candidate carries both observations. A frozen query/detail/export retains that
captured tuple after the next target publication. No money is stored twice in
the product: the abstract token represents the existing Numeric column only.

Mapping: publish_ad_day stamps exact scope/digest/job ID/contract on existing
AdReportDay.evidence in the same transaction as its amount; no new sync owner
or account version. row_set's lateral child branch and fact_coordinates consume
actual row contracts and observations; direct parent state and complete empty
buckets retain authority. The same facts CTE drives SQL membership, sums,
filter/sort and MetricVector. RR freeze_results copies the chosen observation
into existing query rows and publication_map; existing export copying retains it.

Assumptions unchanged: publication is one committed PG transaction under the
existing account advisory lock, query capture is RR, exported tuples are
immutable. The model does not verify those transaction mechanics or promise
new monotonic ordering for overlapping targeted tasks. Real PG publication
regressions bind the existing transaction/fence rules. No claim about unbounded
concurrency, TikTok maturity, permission proof or production deployment.

Real R1 replay: test_targeted_provenance.py first failed on a1=100 with t0;
then passed on t1 + precise C1 digest/contract, preserving a2/account t0.
The source probes independently force old account observation and borrowed
account contract, require business AssertionError, restore an external backup
in finally, invoke cmp, then require unchanged GREEN. Never overlap them with
PG fixtures or another source writer. This binds the model counterexample to
real publisher/query SQL, rather than repairing a model alone.

Evidence and known root-owned P2 failures:
[precise provenance validation](../../../docs/validation/2026-11-14-native-m4a-r1-targeted-provenance.md).
Root must fix the three P2s and perform fresh R2; this sync is not M4a approval.

## JB34 bounded material-instance tracer

sync_commit: `54d55fd6` (product source inspected in the approved M4b shared tree).
Outcome **updated** for material R1/R2/R5. Existing M4a models remain their own
bounded evidence and do not prove material freezing or creative semantics.

```bash
python3 .agents/skills/formal-verify-reporting-data/scripts/material.py
python3 .agents/skills/formal-verify-reporting-data/scripts/material_probes.py
python3 /tmp/tkada-native-series-20261112/m4b-implementation/run-test.py python -m pytest backend/tests/modules/reporting/test_material_instance_tracer.py -q
```

Use the dedicated **M4b** guarded environment; never the historical M4a runner.
Run source probes serially with exclusive product writer ownership and no other
fixture suite. Each probe externally backs up actual source, requires a business
assertion RED, restores in finally, invokes cmp and requires real PG GREEN.
Parent borrowing, BC key rewriting, tenant SQL removal and live page rereading
are bound to actual material query/snapshot/export paths. pytest may render a
rewritten assertion as `E assert`; SQL/import errors are not successful probes.

The model enumerates32 cases: two values for each of tenant, BC, connection,
creative amount and later amount. Captured instance money and full identity
must match the selected creative fact; parent100 stays separate; later source
replacement cannot change page/detail/CSV. Broken parent/scope/freeze modes must
fail. BC/connection are selection coordinates, not additional additive fact keys.
Assumes existing authorized selection and atomic RR capture. It is not a proof
of SQL authorization, unbounded concurrency, multi-instance allocation, five-state
publisher evidence, maturity or production capacity.

Real PG uses creative40, parent100 and another tenant's same remote IDs with700;
source40→90 and directory removal preserve the old payload. Existing live harness
runs original SDK transport→sync Worker→PG/Redis→Uvicorn/Vite/browser→original
export button→original Celery Worker→CSV HTTP, with no business-service/browser
response double. Broader duplicate/special-row/evidence/lifecycle properties
belong to subsequent M4b tickets and require their own bindings. See
[JB34 validation](../../../docs/validation/2026-11-14-jb34-material-instance-tracer.md).

## JB35 identity and creative field evidence

sync_commit: `4c07e42a` (product source inspected). Outcome **updated** for
bounded multi-creative/two-instance/duplicate-reference/five-state/special-bucket
properties. Existing M4a models keep their original scope; none prove creative
platform semantics. Reuse the guarded **M4b** environment above, serial fixtures.

`material.py` now additionally enumerates 12 combinations: 1–3 references per
fact, special versus exact identity, legacy versus observed fifth fact. Five
facts independently carry 10/NULL/DASH/ABSENT/7, aggregating to17 with five state
counts; a separate instance contributes7, total24, or7 when the first belongs to
a special bucket. Fact identity excludes BC/connection/contract/publication.
Atomic committed publication, correct authorized selection, and immutable RR
capture remain explicit assumptions, not new SQL/concurrency proofs.

Run `material.py`, then `--broken-double-count` and `--broken-null-to-zero`;
normal passes and both broken modes must exit1 with a business AssertionError.
Run `scripts/material_identity_probes.py` for real PG binding. It mutates actual
material_query source to duplicate money or replace missing spend with0; requires
business assertion RED, restores external backup in finally, invokes cmp, and
requires unchanged GREEN. Both mutations were rejected. Never run concurrently
with another source writer or fixture suite. Source sensitivity covers the
actual frozen observation/vector results, not only the arithmetic abstraction.

Real bindings: test_material_instance_tracer exercises raw publisher states,
exact targeted scope/version0 without borrowing account observation, duplicate
pages, multi-creative sum17, distinct known MID instances2/8, carousel11,
conflict13, unmapped17, historical MID19, tenant and BC/connection identity,
old frozen observations and same-snapshot CSV. SDK transport replay uses original
sync/export Workers and PGRedis plus actual browser and HTTP. See
[JB35 validation](../../../docs/validation/2026-11-14-jb35-material-identity-evidence.md).
No production scale, unbounded liveness, platform attribution/maturity or strict
parent reconciliation proof; final grain and five CSV classes remain JB36.

## JB36 metrics and reconciliation

Run `scripts/material_metrics.py` (32 bounded cases); each `--broken-scope`,
`--broken-cross-scope`, `--broken-nullzero`, `--broken-parent-total` must fail.
Run `scripts/material_metrics_probes.py` serially through the M4b guarded runner.
It mutates actual material_query/frozen_result owners with external backup,
finally restore and cmp, requiring business assertion RED then PG GREEN.

Binding: material_query's strict denominator merging and reconcile_parent use
independent FULL_PARENT_AD facts, original selected filters and per-field
contract/coverage checks; frozen_result aggregates ordinary instances only;
exports serializes the same immutable five row kinds. Native creative windows,
traffic and maturity remain UNCONFIRMED: no native monetary difference is
proved. The model assumes identity, atomic RR capture, honest complete publisher
coverage and the existing 30-minute observation compatibility window. It does
not prove SQL authorization, arbitrary concurrent histories, platform support,
TTL or export lifecycle; JB37/JB38 retain those boundaries.

JB36 sync source: `981579bc` (material_query, frozen_result, MetricCatalog,
exports and actual PG/browser/Worker tests inspected). Source probes last passed
against these arithmetic/selection owners; later residual observations and
frontend formatting do not alter their mutated branches. Final browser replay
also checks the enriched residual evidence. No claim about JB37/JB38 lifecycle.
