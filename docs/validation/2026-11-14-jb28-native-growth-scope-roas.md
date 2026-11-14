# JB-28 scope/ROAS — WIP, not accepted

Start: `d72d5837967fc5619f2dcbd8f5a577f2a94f6ae1`.
Branch/tree: `feat/jb24-native-growth-m4a`, existing shared integration tree.
Only this ticket changed; no Linear writes, child agents, model changes, checkout,
main merge, push, PR, deployment, production migration, or real platform calls.
This is a partial local implementation, not completion of JB28 or M4a. JB29 must
not start on this evidence. No external Spec/coordinator files were changed.

## Implemented portion

- Daily ReportingData SQL preserves denominator-null flags through rb/rv, so
  filtering and ordering cannot compute a ratio from an incomplete divisor.
- MetricVector and SQL summary distinguish numeric denominator gaps from a stale
  coverage flag. Known revenue retains the entire available same-scope spend:
  20/(10+90)=0.2 PARTIAL; missing spend returns MISSING; zero spend returns NULL.
- Account/drama source routing now reads Campaign selected-day facts, including
  direct authority, rather than the original derived-only shortcut. Existing
  Campaign membership/naming grouping remains. This switch is not yet safe to
  accept for mixed source/contract cases: separation and aggregate provenance
  are pending. Do not release this intermediate commit independently.
- Material query ratios keep their existing behavior; strict denominator logic
  is explicitly enabled for the daily object/account/drama path. Business and
  automation inputs, STORED_KEYS/DERIVED_RATIOS, and derived writers are untouched.

## AC matrix (numbering follows full JB28 ticket)

| AC | Evidence/status |
|---|---|
| 1 same plan across dimensions | Partial: real PG Campaign threshold/row, Account row+summary; full five-view demonstration pending |
| 2 whole-object attributes | Existing selector retained; dedicated complete matrix pending |
| 3 keyword scope text | Pending UI explanation and semantic audit |
| 4 subset cost5 / full-parent40 | Pending; no implicit child meaning added to ids |
| 5 direct authority / old contracts | Existing dash/empty regression passes; old/mixed contract separation pending |
| 6 account/drama Campaign set | Source routing implemented; mixed-source and aggregate provenance validation pending |
| 7 10/90 +20/90 | Real PG summary 1.1 passes; explicit parent-child-addition mutation pending |
| 8 partial/full/zero divisor | Real PG 0.2 PARTIAL, missing divisor MISSING, zero NULL pass; ZERO_DENOMINATOR reason pending |
| 9 compatible buckets | Existing currency separation passes; source/contract/window bucket coordinates pending |
| 10 platform single observation | Previous implementation retained; aggregation observation expansion pending |
| 11 .801 versus .8 | Existing SDK/levels seam retained; new explicit independent comparison pending |
| 12 D0/calendar/hour | Previous contract retained; R8 scope model and new cases pending |
| 13 NULL threshold/order and visible partial | SQL rejects missing divisor; UI reasons/partial display pending |
| 14 old consumers | No business/automation owner changed; material regression31 includes old behavior; explicit consumer regressions pending |
| 15 PG plan/performance | SQL arithmetic exercised on real PG; EXPLAIN, first/page timing and source-scope plan evidence pending |
| 16 mutations | Three executed; remaining required subset/source/currency/ratio/addition/missing-zero probes pending |
| 17 formal scope/Decimal | Six bounded arithmetic examples only; exhaustive R2/R5/R6/R8 model NOT implemented |

## Actual commands and results

Commands run from integration root. `RUN` below means
`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`.
Dedicated MMF guarded loopback PG/Redis; no env printed, no dependency reinstall,
no FLUSHDB. The first two test invocations briefly overlapped by mistake; they
are not the final serial evidence below.

- Initial real PG regression: two intended failures, missing divisor computed11;
  Account direct110 was absent. A first fixture typo used snapshot.summary and
  failed AttributeError; corrected to public page.summary before judging red.
- `RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_dimension_query.py backend/tests/modules/reporting/test_native_levels.py backend/tests/modules/reporting/test_native_publication.py backend/tests/modules/reporting/test_publish_watermark.py -q --tb=short`: **107 passed**, 17 prior deprecation warnings,42.42s, serial.
- After explicitly preserving material legacy ratios: `RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_material_query.py backend/tests/modules/reporting/test_dimension_query.py -q --tb=short`: **31 passed**,4.44s.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/denominator.py`:
  six bounded examples PASS; broken-denominator gives expected counterexample.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/denominator_probes.py`:
  Python incomplete-divisor mutation RED (11), SQL mutation RED (wrong threshold
  member), account-derived-summary mutation RED (110 absent); each restored bytes
  from external backup in finally and reran six green. Account probe initially
  survived because test inspected row only; added independent summary110/spend100
  expectations, then mutation failed. No expected money was weakened.
- Scoped Ruff check/format PASS. No frontend source changed; reuse matching
  JB27/JB26 build evidence. Current real SDK/HTTP/PG/Redis/worker/page seam ran via
  native_levels in107; this does not claim new UI AC implementation.
- `RUN mypy --config-file backend/pyproject.toml backend/app/modules/reporting`:
  still410 errors. Exact start backend was extracted with git archive into an
  external temporary source archive (no new Git checkout) and same command run;
  normalized diagnostics (line numbers removed) have no additions/removals.
  Strict mypy is not green. Archive extraction required original3.14 interpreter;
  system Python lacked extractall(filter=...). Softlinks were preserved.

One existing test expectation changed with an explicit independent contract:
`test_summary_counts_incomplete_advertisers_and_dates` expected PARTIAL ROAS
although one scope account's spend is unknown. JB24 R6/JB28 AC8 require missing
ratio, so the test now asserts NULL and MISSING while retaining partial spend10,
row count2, and incomplete account/date assertions. The new real PG negative
threshold test and both denominator mutations reject calculating that ratio.

## JB29 handoff is NOT ready

Do not freeze the current payload as the completed M4a contract. Existing
MetricVector values/availability/provenance, ReportRow.display and three-layer
query dependency remain available, but source/contract/scope bucket coordinates,
aggregate provenance, per-observation platform ratio data, and explicit reasons
still need JB28 completion. JB29 owns frozen_metrics/contract/publication-map
persistence and old-snapshot handling; JB30 owns CSV. This work adds neither and
makes no claim of values frozen across refreshes. Remaining ACs above must be
implemented and accepted before the root opens JB29.

## Fresh-context continuation from 854b86ec (still not accepted)

Authorized Sol/high, same branch and shared integration tree, no child agents.
Initial HEAD matched `854b86ec8800b39f9ccbfe26223f400f4b18c9f9` exactly;
`.venv` and `node_modules` untracked symlinks retained. No other checkout, main,
Linear, external coordinator files, real platform, push/PR or deploy touched.

### Changes and initial failures

- Real PG independently reproduced direct Campaign40 + next-day child20 merging
  into60. `_facts_cte` now groups level/source/contract/currency before row bounds,
  sort and summary. Page hydration calls the same SQL facts builder with page IDs
  pushed into the fact predicate, replacing its separate daily `_aggregate`
  path. Account/drama follow selected Campaigns. Material retains its own path.
- `MetricVector` adds optional `level`, `source_kind`, `contract_revision`, and
  `reasons`. `bucket_key` preserves these coordinates when merging. Unknown direct
  contracts remain separate and amounts are NULL/CONTRACT_MISMATCH; thresholds
  cannot admit them. `semantics_for` currently explains only native-growth-day-v1;
  a historical interpretable revision registry is not implemented.
- `ReportingFilter.ids` remains current dimension/whole-object filtering. New
  canonical `AdContributionScope(kind='ad', ids=...)`, only allowed for parent,
  account and drama dimensions, restricts AdReportDay before aggregate SQL.
  GET ads exposes `ad_contribution_ids` separately. Filter digest includes it.
  Independent PG test keeps selected Ad spend5 with parent revenue40 inapplicable;
  no parent40/5, no expansion to parent spend50. Existing material contribution
  queries remain separate; creative/material scopes are not added to this union.
- Platform0.801 and local40/50=.8 remain separate. Multiday platform value is NULL
  NON_ADDITIVE, with expandable per-day fields/Decimal strings. This check publishes
  synthetic rows via the real publisher, not SDK; the existing levels harness
  separately exercises SDK transport → worker → PG/Redis → HTTP → browser.
- UI now states “匹配对象的全部数据”, explicit Ad subset reason, bucket coordinates,
  visible “部分” labels, ratio missing/zero reasons and expandable observations.
  Summary labels source/level/contract when buckets split. No frontend formulas.
- First extension fixture used invalid ad_row keyword `campaign_id`; corrected to
  its existing `campaign` argument before judging red. Plan experiment first used
  snapshot.id instead of snapshot.snapshot_id; corrected without changing expected
  money. An inherited exact report_source assertion caught replacing its enum
  with prose: restored enum and moved detailed coordinates into report_coordinates.
- Parent empty page fallback now keeps all amounts NULL instead of fabricating
  spend0; its full source/contract empty-bucket provenance still needs closure.

### Executed evidence

`RUN` is the guarded dedicated local runner defined above. Suites run serially;
no FLUSHDB, reinstall or other-process termination. Build is independent of PG.

- Initial focused source regression RED: expected two buckets20/40, observed60.
  Initial explicit subset check RED: canonical filter rejected unknown contribution.
- Focused daily/material/dimension checks33 passed before subsequent additions.
  Added unknown-contract and platform-observation checks: scope suite10 passed.
- Broader six-file run:118 passed/1 failed solely for changed report_source enum;
  fixed the product field rather than weakening its old assertion.
- Four new scope source probes: concrete assertion RED, finally byte restoration,
  then GREEN each. The first source-collapse probe caused an invalid SQL GROUP BY
  constant; rejected as evidence and replaced with a valid SQL expression. The
  final probe detects wrong monetary buckets, not an SQL exception.
- Three inherited denominator probes rerun after refactoring: Python divisor11,
  SQL erroneous threshold membership, account derived summary missing40/110 all
  rejected; external backups/finally/byte equality and scope-suite11 green each.
- Actual `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on two synthetic Campaigns:
  members planning3.187ms/execution1.222ms; summary1.086ms/.293ms. Real query first
  page39.214ms, cursor page13.887ms; stable order c0,c1 and summary1.1. Full synthetic
  plans are `2026-11-14-jb28-plan-synthetic.json`. This is a small plan experiment,
  not production capacity evidence. A representative large-cardinality plan is pending.
- Mypy410 errors; normalized diagnostics equal inherited `mypy-final.log` (zero
  additions/removals). Strict type checking remains non-green. No claim of a new
  exact-start archive baseline; the previous author's exact-start comparison is inherited.
- Build/tsc and final six-file suite results are appended below after completion.

### Superseding AC matrix and next work

| AC | Current evidence and remaining boundary |
|---|---|
|1|Same facts SQL feeds source grouping/bounds/sort/summary/page hydration; five-view SDK/browser demonstration still incomplete.|
|2|Existing whole-object selectors preserved; full attribute-filter matrix pending.|
|3|Visible whole-object scope; explicit Ad contribution filter implemented. Material/creative real-entry audit and all scope combinations pending.|
|4|Real PG parent40/selected Ad spend5 regression green, matching source probe rejects ignored filter. HTTP/browser subset demonstration pending.|
|5|Dash/empty authority inherited; unknown direct contracts NULL/CONTRACT_MISMATCH added. Complete-empty coordinate ownership and old-contract cases need closure.|
|6|Campaign-only account/drama source grouping regression green; aggregate provenance and ambiguous naming audit pending.|
|7|Summary1.1 and inherited three-level anti-overwrite green; explicit cross-layer addition probe pending.|
|8|Complete/partial/missing/zero divisor cases and reasons implemented; full known/missing count provenance pending.|
|9|Source/level/contract/currency buckets separate; timezone MIXED inherited. Explicit per-range/window/count aggregate provenance pending.|
|10|Single/multiday platform Decimal regression and first-day-as-multiday probe green; aggregate account/drama observations still pending.|
|11|Explicit .801 vs .8 regression green; SDK/HTTP/page precision harness inherited and rerun.|
|12|D0 existing labels unchanged; generated midnight/calendar/hour model and matching product regressions pending.|
|13|Visible partial/scope/reasons added; null threshold regression green; five-view missing/same-name/paging UX demonstration pending.|
|14|Material tests green; consumers unchanged. Explicit full named-consumer regression audit pending.|
|15|Real small PG plans and actual first/cursor page evidence added; representative scale and full vector/provenance plan evidence pending.|
|16|Seven source probes executed (three inherited, four new); cross-layer addition, average ratio, currency, cross-source division, missing-to-zero probes still pending.|
|17|Existing six examples only; generated R2/R5/R6/R8 model remains unimplemented.|

Continuation must start by finishing actual provenance, complete-empty coordinates,
consumer/entry audit and full required formal model, then finish the five-view
live harness. Do not mistake these tests for all17 AC closure. The current session
reaches a context boundary with these explicit gaps; preserve this checkpoint
for a fresh implementation context. JB28 remains unaccepted, JB29 must not start.

JB29 can inspect the added MetricVector coordinates/reasons and canonical
ReportingFilter contribution, but **must not freeze them as accepted contracts**:
aggregate provenance/stamps/counts/windows are incomplete. Snapshot persistence,
publication-map freezing and detail/page immutability still belong to JB29;
CSV remains JB30. No migration or those consumers were implemented here.

Final continuation checks: six reporting suites **120 passed**,20 existing
warnings,37.48s, serial, after all source probes restored. Exact command:
`RUN python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_dimension_query.py backend/tests/modules/reporting/test_material_query.py backend/tests/modules/reporting/test_native_levels.py backend/tests/modules/reporting/test_native_publication.py backend/tests/modules/reporting/test_publish_watermark.py -q --tb=short`.
Scope suite contains11 tests including the SQL/paging experiment. Existing
native_levels real HTTP/worker/PG/Redis/browser harness ran in this120; it is
**not** the missing five-view/subset/partial UI scenario. Formal/demo frontend
`bun run build` (including tsc) passed again after final UI changes. Seven changed
Python/script files passed Ruff check and format; diff check passed. No final
claim of exhaustive formal coverage, whole-ticket acceptance or production use.
