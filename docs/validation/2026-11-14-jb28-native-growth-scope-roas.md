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
