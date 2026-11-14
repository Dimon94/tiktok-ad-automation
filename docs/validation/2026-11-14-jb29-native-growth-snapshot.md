# JB29 Native Growth query observation candidate

Scope: JB29 only, Sol/high, one writer in `feat/jb24-native-growth-m4a`.
Start: `47359643e6662568c124d314f0684d943e3bcbb0`. No branch/checkout,
tracker, push, merge, deployment, production migration or real platform operation.
The original untracked `.venv` and `node_modules` links remain. JB27/JB28 local
root acceptance releases this dependency; Linear status/edges are untouched.
This is a local implementation candidate for independent root review, not Done
and not M4a completion. CSV remains JB30.

## Implementation and consumer boundary

`insert_snapshot_rows` uses its existing facts/mb/fr/ordered SQL plan for
thresholds, membership, order, summary and a narrow selected aggregate projection.
The same INSERT statement writes that projection into the existing snapshot rows.
Within the original RR single-flight transaction, `freeze_results` formats at
most 100 selected rows at a time using `materialize_rows(selected_facts=...)`.
It never reloads money or chooses a second source. SQL keeps large-set membership,
filtering, sorting and sums; Python only formats selected projections. Metadata
needed for report names travels through that same projection. Decimal amounts
are encoded as text at the SQL JSON boundary and decoded with Decimal.

`QuerySnapshotRow.frozen_result` stores a validated ReportRow: display fields,
actual MetricVectors, availability/reasons, per-metric provenance, contracts,
local ranges, denominator counts, observation stamps and single-day platform
ratio comparison. Temporary SQL aggregates are removed before commit; no raw
SDK response, credentials or long-lived raw version store is added. Directory
configuration is not copied. Account and Drama share the Campaign source path;
material/creative and decision inputs retain their existing owners/contracts.

`QuerySnapshot.result_contract` is the `selected-report-result-v1` revision plus
a copy of the Native metric contract catalog. `publication_map` holds independent
account/kind/day publication coordinates, versions, observed/published times,
status, contract and digest. Absent states remain absent (unknown), never borrowed
from another layer or the current account. Existing 15 minute lifetime and
bounded snapshot-row cleanup apply.

Pages call `frozen_row`; details reauthorize the live directory object and read
the matching frozen row. A supplied snapshot must contain the object. Independent
details create/return an object-scoped query for the current account-local day.
Directory name/configuration/status remain current and are explicitly labelled
`CURRENT_DIRECTORY`; the report carries its own snapshot/date range. No frontend
ratio/source calculation was added. Source publication only displays “有新数据，
点击加载”; idle timers no longer replace daily report amounts automatically. Material retains its original refresh policy.

JB30 must consume `frozen_row(QuerySnapshotRow)` in stable_sequence order, copy
`result_contract`, `publication_map`, snapshot.filters/filter_digest and the
frozen vectors/provenance/display ratio observations into its existing export
job. **Do not use materialize_rows or the current `_rows_for_snapshot` export
implementation for JB30.** The present CSV path is unchanged and is not accepted
as frozen CSV. `metric_buckets` on the storage row is emptied after formatting;
the final payload is `frozen_result.metric_buckets`. Public snapshot fields expose
contract/map for review. Decimal HTTP values remain strings; NULL stays NULL.

## AC evidence matrix

1. Same SQL selection/summary/freeze, actual RR and single-flight: real PG native
   snapshots/scope tests and query_reuse concurrent 20-request/lease/cancellation
   suite. Three-level SDK/Worker HTTP fixture remains the main seam.
2. Frozen vectors, scope, contract copy and independent publication map: snapshot
   tests, inherited JB28 vector assertions rerun on this path, live HTTP payloads.
3. First/continued pages and snapshot detail: exact vector/whole-row comparisons,
   including a continuation after publication; live browser/detail HTTP equality.
4. Money refresh: 10/90 -> 100/900 PG examples; SDK Campaign 40 -> 80 while old
   table/detail remain unchanged; explicit UI new-data action loads 80.
5. Evidence-only digest changes invalidate reuse without money changes; parent
   publication alone changes dependency/new-data version, with Adgroup/Ad's immediate pre-refresh
   publication versions unchanged. An older cached query can legitimately predate
   an already completed retry; its map remains frozen too. All day kinds, derived source revisions,
   directory/link/naming revisions, full filters and contract copy participate.
6. Standalone detail returns its own snapshot/result. Current directory attributes
   are separately labelled; creative details keep their directory-only contract.
7. Scope mismatch, legacy/changed contract, missing frozen row payload and expiry
   explicitly reject with 409. No old amount is reinterpreted from current facts.
8. Every page/detail passes read_snapshot tenant/actor/BC/current account grants;
   detail also uses directory.locate. Real PG page and detail deny after revocation.
   Existing two-tenant/BC dimension tests rerun. Source BC is never authorization.
9. SDK refresh advances only Campaign in the live snapshot example; newer
   Campaign and independently last-published Adgroup/Ad are represented separately,
   not one timestamp. The refreshed current-state versions are checked against
   the actual pre-refresh PG state, rather than an older cached query.
10. Payload size/build/page/detail/22 bounded cleanup batches measured with 2,000
    synthetic Campaigns. Original lifetime/cleanup owner retained; no new store.
11. Local Alembic additive upgrade applied by guarded PG fixture; old rows get
    empty contract/payload and explicitly exit, historical money is not rewritten.
12. Real EXPLAIN ANALYZE/BUFFERS for selection and summary plus actual SQL INSERT
    build timings. Single-flight, takeover, cancellation and cleanup suite rerun.
13. Seven source mutations: live page, live detail, omitted evidence digest,
    Ad-only watermark, legacy contract bypass, scope bypass and revoked grants.
    Each causes business assertion failure (including expected-error not raised),
    restores an external backup in finally with cmp, then passes GREEN.
14. Existing ReportingData R4/R10 48-state and R7 57-state models rerun. R9 adds
    128 bounded two-query/three-layer/evidence observations and a broken-live-read
    counterexample, bound to real PG source mutations. These are bounded models,
    not unbounded system/platform proofs. Formal sync is recorded in its skill.

## Real failures corrected

- Initial RED: refreshing a published bucket changed an old page's ReportRows.
- First live browser failure: narrow SQL aggregate omitted report names. Carry
  selected metadata through the same SQL projection; do not reread live facts.
- Decimal boundary check exposed string arithmetic after removing JSON floats;
  decode selected money to Decimal before the shared vector formatter.
- Expanded concurrency tests found empty Account summary emitting invalid
  level=account. Its actual report source layer is Campaign; corrected at SQL
  member-bucket construction. Five failures were superseded by the targeted
  single-flight/lease/cancellation rerun.
- Reusing coverage-selected states for refresh overview lost other layer states;
  preserve the original three-layer states for overview independently of coverage,
  and exclude creative/account kinds from its three-layer completeness count.
- Standalone metadata test fixture initially omitted required ad_type; this was
  fixture setup failure, not behavioral RED. Capacity instrumentation initially
  omitted a select import; that run is not capacity evidence.
- A later live test compared a cached stale Adgroup query with a completed retry;
  correctly frozen old state and latest query differ. The source-refresh fixture
  now records immediate before/after PG versions to isolate this publication.
- Snapshot browser's initial continuation request omitted the original query
  parameters and correctly received scope mismatch; use its actual query URL.
- Narrow mypy identified four new typing issues; all fixed. One pre-existing
  `_native_detail` Sequence/list annotation error remains. Existing full 410-error
  baseline is not green and was not rearchived or rerun as an acceptance claim.

## Commands and measured boundaries

All PG/Redis commands use:
`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py <command>`.
No FLUSHDB, dependency reinstall, concurrent fixture suites, or unrelated process
termination. SDK urllib3 transport is the only provider double. Synthetic tenant,
authorization and directory fixtures arrange access; they do not seed final UI
responses. The refresh barrier starts the existing enqueue/claim/Worker/SDK
publication path while the browser retains an old result; no business service or
browser response is mocked.

- `python -m pytest backend/tests/modules/reporting/test_native_snapshot.py -q`:
  eight cases (three dimensions, details/rejection, evidence-only, other layers,
  additive legacy upgrade and two-connection RR publication race).
- `python -m pytest backend/tests/modules/reporting/test_native_scope_roas.py backend/tests/modules/reporting/test_dimension_query.py backend/tests/modules/reporting/test_query_reuse.py backend/tests/modules/reporting/test_cleanup_batches.py -q`:
  source/scope, empty/NULL/ratio contracts, actual lease/transaction cancellation,
  and bounded cleanup; final run recorded below.
- `python -m pytest backend/tests/modules/reporting/test_native_levels.py -k 'two_page_adgroup_worker_and_query and False' -q`:
  actual SDK/Redis/Celery/PG/HTTP/Vite/Playwright three-layer chain, stale old
  observation, layer retry, 52-row pagination, five views/subset, frozen details,
  independent detail and source refresh/new-data action. Final run below.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot_probes.py`:
  seven real-code RED/restore/GREEN probes, serial only.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/{check,publication,snapshot}.py`:
  48/57/128 normal cases; snapshot `--broken-read` rejects evidence-only refresh.
- Ruff on changed Python files; frontend `bun run --filter frontend build`
  includes tsc plus main/demo builds. Git diff --check.

Initial complete 2k run: 6,157.17 ms build+first query, 1,313.18 ms continuation,
651.71 ms detail, 11,701,258 bytes sum(pg_column_size(frozen_result)), 62.86 ms
cleanup across 22 calls (100-row bounded batches), no snapshot children remaining.
EXPLAIN member selection 1,348.46 ms and summary 1,622.49 ms. These are dedicated
local PG synthetic one-day measurements, not production capacity, logical JSON
wire size, or universal latency promises. JB28's source-matching earlier 3.49s/
1.30s is historical comparison only. Final source rerun is recorded below.

## Upgrade and rollback constraints

Migration `native_frozen_query` follows `native_publication_coordinates`; adds
JSONB result_contract/publication_map/frozen_result with empty defaults only.
No old row backfill, direct-fact delete, historical amount rewrite or production
migration was performed. A future release must stop/drain old report/projection
writers, apply the reviewed additive migration, then start matching API/Worker
and (JB30) export readers/writers together. Do not mix old publishers/readers
with committed direct facts. Old snapshots exit with 409 and normal TTL cleanup;
completed legacy CSV artifacts are retained by the existing owner.

Automatic downgrade deliberately refuses: rollback needs an independently
reviewed compatible reader/writer plan, snapshot expiry/drain and preservation of
all direct facts. Reverting to an old reader that ignores direct facts is not an
acceptable rollback. Production upgrade/rollback rehearsal remains outside this
authorized local ticket and must not be represented as completed here.

## Final local verification

- Combined PG scope/snapshot/dimension/single-flight/cleanup suite: **109 passed,
  49.35s**, including the 2k case. Final measured 2k: first 8,964.62 ms,
  continuation 1,620.69 ms, detail 750.36 ms; sum(pg_column_size) **11,678,307
  bytes**; 22 cleanup calls 89.43 ms and zero remaining children. EXPLAIN ANALYZE
  selection 2,066.11 ms, summary 2,362.30 ms. Local latency varied between runs;
  freezing adds construction/storage cost. No production capacity claim.
- After explicit malformed-payload rejection and material-plan preservation:
  snapshot + original snapshot-member consumers **10 passed, 2.55s**, including
  a real local upgrade from the prior three-column schema shape. It retains old
  snapshot membership/total and direct amounts, rejects the legacy contract, and
  verifies downgrade refuses without deleting facts. No production rehearsal.
- Final SDK/Worker/HTTP/browser chain: **1 passed, 6 deselected, 57.88s**. Old
  page/detail equality, standalone detail snapshot, prompt-before-load and 40→80
  all passed. Campaign advanced while the actual before/after PG Adgroup/Ad
  versions remained unchanged. Earlier cached stale observations remain frozen;
  requery legitimately includes a retry completed before the callback too.
- Seven source probes rerun against final logic: all business RED → external
  backup/finally/cmp restoration → GREEN. Expected HTTP-error absence is a valid
  business failure; SQL/import errors are not accepted by the harness.
- Frontend main/demo build including tsc passed; changed Python Ruff and
  diff whitespace checks passed. Focused mypy now has only the inherited
  `_native_detail` Sequence/list annotation error; no new issues in the new owner.
- A final real two-connection RR test publishes 100/200 after the SQL snapshot
  INSERT but before formatting; both row and summary must remain 10/20, and the
  next query must see 100/200. Its initial empty result came from an uncommitted
  fixture publication, corrected by explicit commits (not behavioral RED).

Inherited boundaries: JB28's independent numerical/field-state expectations and
unchanged publication/admission/SDK contracts, R4/R10 and R7 source mappings.
Current verification exercises those expectations through the new frozen path;
it does not relabel former static or mock evidence as a new live seam. External
TikTok semantics, production migration/rollback/capacity and final MMF CSV remain
unverified/not performed. Root independent review and JB30 are outstanding;
there is no known intentionally deferred JB29 product AC in this candidate.


Product candidate: `8f986018f3dc389aeebaf8002a82e86e1d1ff947`.
The maintained R9 skill was followed from this head: normal 48/57/128 models
passed, broken snapshot live-read produced the expected AssertionError, and the
complete current snapshot PG suite passed **8 cases in 6.00s**. The controlled
RR race independently passed **1 case in 0.79s** after committing its fixture
publications. Final source-probe restoration/commit is recorded in the skill
maintenance commit; no product byte changes accompany it.

## Root local phase acceptance

Root independently inspected frozen_result, RR single-flight transaction, same-plan SQL projection, page/detail authorization and legacy rejection, additive migration and JB30 boundary. Native snapshot + query reuse checks: 19 passed/5.75s. Normal R9 128 cases passed; broken live-read variant failed on evidence-only refresh as expected. An initial command referenced a nonexistent test_snapshot_members.py and collected no tests; the successful command used test_native_snapshot.py and test_query_reuse.py. Source-matching prior browser/build, seven source-probe and capacity evidence is inherited, not rerun. No new product fix was required. JB29 locally accepted to release JB30; whole-MMF review, CSV/local integration and real production acceptance remain.
