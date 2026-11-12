# JB-6 / M2 historical operation-log evidence — 2026-11-12

## Scope and source

Read the complete latest JB-4/JB-6 Linear bodies and all comments (both empty).
JB-4 updated 2026-11-12 11:24:57 UTC; JB-6 updated 11:25:07 UTC. Implementer made
no Linear writes. Nowledge targeted search returned no additional M2 decision.
Read AGENTS, CONTRIBUTING, the parent GLOSSARY (no child GLOSSARY/test standard
exists), config/README, approved operation-log design and existing business owners.
Used tdd, principle-test-behavior-not-implementation,
create-formal-verification-skill, git-commit.

Authorized sole-writer tree: `projects/tiktok-ad-automation.wt/fix-jb6-historical-log-evidence`;
branch `fix/jb6-historical-log-evidence`; baseline
`af24c3b968b9fa5cdbd8c55c9e5df4f00478df6f`.
Original untracked `.venv` and `node_modules` symlinks were preserved. No new checkout,
branch, shared dependency changes, lockfile edits, migration, API or business flag.

Only product owner changed: `operation_logs/evidence.py:frozen_sources`.
The selected observations are unchanged. Each account's IANA local anchor dates
extend only that account's coverage interval. Original start/end remain the
requested calculation interval. Newly selected coverage/export content flows through
the existing semantic account signature, UTC footprint and source signature.
Revision/export IDs remain provenance, not semantic identity. Build/restore,
formula, source snapshot, current checks, lifecycle, query and CSV owners are reused.
Export reads retain tenant and now explicitly account filtering.

Exact repaired source sync: unchanged af24 baseline plus evidence.py Git blob
`6d081745147fa008c3d5ed0fa65e57f6c8b1174c`. The generated M2 skill records this same
mapping; it does not claim the old af24 code already contained the repair.

## Isolated environment and commands

Owned Docker `jb6-pg` (postgres:18-alpine, DB `jb6_test`, random loopback port)
and `jb6-redis` (redis:7-alpine, app DB 0 / test DB 1). Python 3.14.2 environment
`/tmp/jb6-venv`; local-only configuration `/tmp/jb6-env.sh`, mode 0600.
`APP_ENV_FILE=/dev/null`; explicit synthetic admin/encryption/app configuration;
cloud/provider credentials absent; all automatic business switches disabled.
Only synthetic tenants/accounts/logs; no TikTok, cloud or production calls.
Worker integration substitutes only SDK/archive transport. Main vertical acceptance
uses real PG, actual admin HTTP API, actual advance_run and actual CSV writer/download.
The failure injection binds worker session factories to the rollback fixture and
raises once at its calculation boundary; it does not mock publication/query results.

```sh
UV_PROJECT_ENVIRONMENT=/tmp/jb6-venv uv sync --frozen --package app --python 3.14 --offline
source /tmp/jb6-env.sh
cd backend
python -m pytest tests/modules/operation_logs/test_daily_evidence.py tests/modules/operation_logs/test_historical_evidence.py tests/modules/operation_logs/test_store.py tests/modules/operation_logs/test_replay.py tests/modules/operation_logs/test_parser.py tests/modules/business/test_historical_log_rebuild.py tests/modules/business/test_operation_logs.py tests/modules/business/test_projection.py tests/modules/business/test_incremental_rebase.py tests/modules/business/test_prefix_progress.py tests/modules/business/test_api.py tests/modules/business/test_exports.py tests/modules/business/test_concurrency.py tests/modules/business/test_export_integrity.py -q --tb=short
```

Result before the final five targeted cases: **166 passed, 1 failed in 129.94s**,
`/tmp/jb6-suite.log`. Failure is existing
`test_long_pending_continues_bounded_repair_rotation`: fixture supplies
`repair-000` without an AdvertiserAccount timezone, then unchanged
`operation_logs/planning.py:plan_logs` raises KeyError. Swapping only evidence.py
back to af24 and rerunning that exact case reproduced the same failure
(`/tmp/jb6-baseline-failure.log`, 0.26s); repaired file restored with cmp PASS.
No unrelated scheduler/fixture fix and no claim that the entire suite is green.

Final targeted run (new cases plus real log worker transport-boundary integration):

```sh
python -m pytest tests/modules/operation_logs/test_historical_evidence.py tests/modules/business/test_historical_log_rebuild.py tests/modules/operation_logs/test_worker.py -q --tb=short
# 18 passed in 3.46s; /tmp/jb6-final-targeted.log
```

After all real-code mutations were restored:

```sh
python -m pytest tests/modules/operation_logs/test_historical_evidence.py tests/modules/business/test_historical_log_rebuild.py -q --tb=short
# 14 passed in 1.71s; /tmp/jb6-restored.log
```

Ruff check, Ruff format --check, py_compile on the product file, two new test files
and model: PASS. `git diff --check`: PASS. No full-repository typing/build claim.
Existing baseline quality failure is explicitly retained, not counted as M2 red.

## Red / green and real-code probes

The first new real-PG test ran before product edits. Approved 2026-10-01 example:
anchor 2026-09-30T12Z, independent complete Sep 30/Oct 1 exports, C=100, V0=60,
bid=1.2, events=300, no later anchor/change. It failed at `amount == '50'` with
NULL, UNKNOWN, LOG_CHAIN_INCOMPLETE (`/tmp/jb6-red.log`). Same case then passed
(`/tmp/jb6-green.log`). Expected 50 comes from the approved example, not current code.

Six scratch mutations of the actual owner ran, each copied back from
`/tmp/jb6-evidence-final.py` and checked with `cmp` before continuing:

| Mutation | Observable rejection | Log suffix / result |
| --- | --- | --- |
| Keep left=start | Approved 50 becomes NULL; old early evidence omitted | left-boundary: 9 failed / 5 passed |
| Use UTC date | Tokyo right day and New York left day omitted | utc-date: 2 failed / 3 passed |
| Omit log content/footprint from signature | old auto incorrectly current, new candidate reused | source-signature: 2 failed |
| Remove coverage tenant predicate | foreign early export changes signature and dates | tenant: 1 failed |
| Coerce UNKNOWN to ZERO | true gap, absent anchor, corrupt/ambiguous bid lose NULL | unknown-as-zero: 5 failed |
| Refreeze latest during restoration | old frozen digest/content changes after new export | live-restore: 1 failed |

Logs `/tmp/jb6-mutation-<suffix>.log`; runner `/tmp/jb6-mutations.py`;
restore confirmations `/tmp/jb6-mutations.log`. All failures are behavior assertions,
not imports/environment failures. Final file cmp PASS; final 14 cases green.

The first vertical test expected REBASE on a changed operation-log signature.
Inspection confirmed existing prefix owner treats that structural source change as
SUPERSEDED; JB-4 expressly permits rebase **or invalidation**. The test now asserts
SUPERSEDED, unchanged old publication, and a new administrator candidate. No product
lifecycle or Spec change was made to satisfy this test correction.

## Acceptance and transition/invariant map

| Contract | Dynamic evidence |
| --- | --- |
| AC1; T1/T2/T4 | Approved PG red/green; new admin vertical test returns 202, freezes early export, advances to COMPLETE, queries and downloads 50. Bid 1.2, ESTIMATED_PENDING, qualification pending issue, correct run IDs. |
| AC2; R1 | Historical PG cases cover quarter/year, NYC UTC/local difference, NYC 23/25-hour exported windows, Tokyo right boundary, two independently frozen accounts in UTC/Tokyo, combined identical per-account dates/signatures, later selected anchor and excluded obsolete earlier export. |
| AC3; R2/R3 | Frozen gap remains UNKNOWN with identical evidence ID/digest/data after new export. New frozen becomes 50. Early content changes both account and source signatures/current; revision/export-ID churn with equivalent content does not. Existing empty-tail test covers real UTC-footprint extension semantics. Final verification rejects changed early source before publication. |
| AC4; R4 | Real PG missing anchor/export, interior gap, corrupt bid and same-minute ambiguous changes remain NULL/UNKNOWN; spend=0/events=299 remain 0. Existing pause/negative-operation and replay threshold cases pass. No formula copied as test oracle. |
| AC5; T3/T5/T6/T7 | New vertical worker exception marks FAILED; retry is fresh, original failed row unchanged. Early-source update causes SUPERSEDED before publication. Existing projection, incremental rebase, prefix and concurrency tests cover bounded restore, query-budget rollback, coalescing, config/auth fences and terminal states. One unrelated missing-account fixture failure is recorded above. |
| AC6; R6 | Foreign tenant/BC same named accounts/objects with different bid cannot change frozen content/signature or fill a gap. Separate account ranges don't spread. Admin unauthorized BC rejected; existing viewer/operator, evidence account denial and pending-export downgrade tests pass. Real log worker revocation case passes. |
| AC7; R5 | Old run, BusinessDay and OperationDayEvidence full contents unchanged after new publication; previous actual CSV is byte-identical after failure, invalidation and successful new publication. Query and new CSV point at new run/50; same-source COMPLETE reused. |
| AC8 | Own M2 finite model, real negative controls, real-code mutants, actual PG/Redis targeted regressions. M1 model is not used as M2 evidence. |

Old-value tests compare full structured values and full bytes (stronger than only
a digest comparison); evidence digest itself is also compared. An existing source
identity can legitimately yield a new provenance ID without a semantic revision.

## Bounded model

Generated and followed `.agents/skills/formal-verify-operation-log-freeze/SKILL.md`
before product implementation. Run from repository root:

```sh
python3 .agents/skills/formal-verify-operation-log-freeze/verify.py
```

Python stdlib exhaustive enumeration: **78,732 source transitions PASS**. Two
accounts, three relative local dates, one requested day, a fixed selected anchor
per account, absent/two semantic content states, every one-source correction and
failed/superseded/verified publication outcomes. Negative variants for early boundary,
source signature, global account expansion, latest-source restoration and failed
publication all rejected (`/tmp/jb6-model.log`). Actual product mutations above bind
the model obligations to executable code. Python 3.11.2 and task 3.14 work without
extra dependencies; no unbounded prover dependency was added.

Assumes atomic DB transactions, immutable exports/observations and fixed selected
anchors. Semantic content abstracts storage IDs/revisions; real PG replay tests cover
their distinction. IANA conversion/DST, formulas, permissions, Redis concurrency,
large ranges and file durability are outside this model; actual tests cover their
stated cases. This is bounded verification, not whole-system proof.

## Handoff / remaining boundary

Owned jb6-pg/jb6-redis and `/tmp/jb6-venv` remain available for root/reviewer; environment
file contains local-only generated configuration and must not be committed or printed.
Shortest continuation: source `/tmp/jb6-env.sh`, run the 14-case restored command from
backend, then the model command from root. Existing broad results remain valid for
the unchanged product source; no reason to rerun all tests just for documentation.

No push, PR, deployment, browser acceptance, production/BC data reconciliation or
real external log retrieval was performed. Actual HTTP query/download acceptance
is complete; no claim of a new real-browser session. Independent Standards/Spec
review and integration belong to root. Known baseline fixture failure remains open.
