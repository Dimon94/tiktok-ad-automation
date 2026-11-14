# JB30 Native Growth CSV — local implementation candidate

Start `696d37c949a184d5ff9104c26cf2c8bdde5aa6c4`, branch
`feat/jb24-native-growth-m4a`, unique writer Codex gpt-6.1-sol/high.
Only the approved shared integration tree was written. Original `.venv` and
`node_modules` symlinks are unchanged. No checkout/branch creation, tracker write,
merge, push, PR, production migration, deployment or real provider/ad write.
JB25–29 local root acceptance releases dependencies; Linear status/edges remain.
This candidate is **not Done**, independent Standards/Spec review and root MMF
integration remain required.

## Consumer contract

The existing `create_export`/ReportExport/outbox/`reporting.export`/HTTP download
owners remain. For Campaign, Adgroup, Ad, Account and Drama, creation reads
`frozen_row(QuerySnapshotRow)` in stable_sequence order, copying the complete
ReportRow, MetricVectors/provenance, filters/filter_digest, result_contract and
publication_map into the existing task. `QuerySnapshotRow.metric_buckets` is not
used. The worker yields the task's frozen payload; it never materializes current
facts or directory names. Material retains its existing member/materialize path;
creative, business exports, automated rules and revenue writers are unchanged.

CSV keeps the existing numeric columns and balance observation columns. Frozen
exports label total as “Native Growth 总展示价值（平台原字段）”, D0 as “Native Growth D0
收入（点击后24小时）”, and calculated ratios as local ratios. Decimal values are text
at the JSON/CSV boundary with no float conversion. NULL becomes an empty cell;
zero remains zero. Structured columns `metric_vector` and `report_display`
preserve availability/reason, level/source/revision, original field or formula,
actual scope digest, observation records/local ranges, known/missing and denominator
counts, single-observation platform ratios and their NON_ADDITIVE aggregate
status. Metric contracts retain UNCONFIRMED traffic and click24h; no natural+paid
semantic confirmation is invented. `result_contract`, `publication_map`, `filters`
and `filter_digest` copy the same query context, including each account/level/day
watermark. Text is CSV-formula escaped. No raw SDK response or credential is copied.

The task lives independently of the query's 15-minute TTL; its own existing
24-hour lifetime, object key, current tenant/actor/BC/account checks and cleanup
remain. Cleanup also removes the copied rows/contract/map. Duplicate creation
uses the original idempotency key; retries use the same payload. A completed
legacy CSV is returned unchanged. An unfinished non-material legacy task fails
with `report_export_contract_incompatible_reexport` in public coverage, and the
UI failure message asks the user to query and export again. Legacy snapshots
still reject before creation. No compatibility reinterpretation is added.

## Acceptance criteria (JB30's 16 items)

| AC | Evidence and boundary |
| --- | --- |
| 1 three CSVs through existing HTTP/Worker/PG/Redis | Extended existing native_levels/live_campaign_workspace; real Uvicorn HTTP query/create/download, canonical outbox payload to actual Celery worker on a unique real Redis queue; only SDK transport is replaced. |
| 2 copied result/contract/scope/map | PG export regression checks exact frozen ReportRows/contract/map; source update + snapshot deletion before generation succeeds. |
| 3 page/continuation/detail/CSV equality | SDK fixture has 52 Campaigns; collect same-snapshot continuation, compare every CSV vector/display/map; original browser checks page/detail across source refresh. |
| 4 Decimal/NULL/zero/privacy | Independent 0.100000000000000001/0/0.3/dash PG expectations; CSV blanks and zero asserted; precision and NULL-to-zero source probes fail. |
| 5 platform ratios non-additive | Copies validated JB28/JB29 result rather than recalculating; vector/display exact comparison. Inherits existing single-day and aggregate NON_ADDITIVE expectations; does not average ratios. |
| 6 old failed layer/missing/comparability | Inherits unchanged JB26–29 failure, DASH/ABSENT/LEGACY_UNKNOWN, empty, PARTIAL, currency/timezone and scope tests; CSV carries their exact vectors. Does not claim every combination rerun through live HTTP. |
| 7 source refresh/repeat/retry | SDK Campaign source changes 40→80 while export remains original 40; create duplicate same ID; PG retry regenerates identical bytes after refresh. |
| 8 tenant/BC isolation and revocation | Existing same-ID tenant/BC fact selection remains unchanged; targeted PG isolation rerun; new wrong-BC export download and revoked repeated/new create/download checks, source permission/scope probes. Final live fixture checks revoked page/detail/create/download. |
| 9 legacy files/tasks/snapshots | Real Worker retains completed original bytes, fails pending old contract; JB29 snapshot 409 contract gate unchanged/inherited. |
| 10 additive local upgrade | Separate database: exact pre-M4a app code loaded from git blobs, actual old publish_ad_day commits 10/20 then process exits; Alembic baseline→head; asserts unchanged money, LEGACY_UNKNOWN and no parent backfill; same-version complete SDK/HTTP/Worker/export seam passes there. No manual PG updates substitute for this rehearsal. |
| 11 rollback/other owners | Native migration downgrade entrypoints refuse; direct rows retained. No automatic drop or old-reader fallback. Other money/product paths unchanged; original material CSV checks pass. This is rollback refusal/preservation verification, not a production rollback rehearsal. |
| 12 T1–T11/R1–R10 | Matrix below; reuse matching evidence rather than repeating exhaustive suites. |
| 13 real seam | No browser response/service/repository mocks or seeded final UI results. Auth/directory fixtures are synthetic arrangements; values originate at SDK transport and go through original sync Worker/publish/query. |
| 14 formal suite | R9 128-observation model extended to independent export lifetime; normal passes, broken-export fails. Six real source probes require business assertion RED, external backup/finally/cmp restoration and GREEN. Existing R4/R7/R10/source/Decimal models and probes inherit matching evidence. Finite model is not a whole-system proof. |
| 15 measurements | Existing three-layer requests/checkpoint/lock/backlog and 2k SQL/snapshot evidence inherited; new CSV/payload/timing below. Local synthetic evidence only, no production capacity forecast. |
| 16 no provider/ad/production writes | Only dedicated local PG/Redis and synthetic fixtures; no platform call outside SDK transport double. Customer originals excluded. |

## T/R rollup

| Contract | Current or source-matching evidence |
| --- | --- |
| T1 queued refresh | JB26/JB27 real refresh HTTP to existing three-kind enqueue; current live seam reuses it. |
| T2 claim | JB27 original claim/fence/Redis Worker, unchanged; live seam consumes it. |
| T3 reading continuation | Two-page SDK transport, checkpoint; JB27 quota yield and only missing page retry inherited. |
| T4 publish | Three direct levels and own watermark; current SDK seam, unchanged JB27 atomic PG tests. |
| T5 failed attempt | JB27 failed Adgroup second page keeps old facts/stamps, source matching. |
| T6 retry | JB27 only failed layer/gap retry, no reread successful layers, source matching. |
| T7 refresh published | Current SDK Campaign 40→80; JB27 late/replay/empty and publication races inherited. |
| T8 freeze query | JB29 RR SQL snapshot/projection evidence inherited; new export consumes it. |
| T9 page/detail/export | Current full chain and new frozen PG CSV/source probes. |
| T10 expire/incompatible | JB29 query gate inherited; current export legacy failure, source snapshot deletion and bounded payload cleanup. |
| T11 new query | Current browser explicit new-data action loads 80, original export remains 40. |
| R1 | Original current grants/tenant/BC route owner retained; new export revocation and scope probes plus inherited same-ID tenant/BC fact isolation. |
| R2 | Exact frozen values/states copied; current NULL/zero/precision assertions and probes; JB28 states inherited. |
| R3 | Frozen field/formula/contract and semantic metadata copied, no new catalog interpretation. |
| R4 | No writer/selector changes; JB26 direct-dash/empty authority and 48-state model inherited. |
| R5 | Task copies frozen filters/digest/scope; JB28 contribution/subset + JB29 scope gate/probe inherited. |
| R6 | No new aggregation or ratio computation; JB28 independent denominator/mixed-source/currency and generated/probe evidence inherited. |
| R7 | JB27 57-state model + actual PG/Redis race/probes unchanged; three independent maps copied. |
| R8 | Existing account-local ranges, D0 click24h/unknown total cutoff copied; no hourly borrowing. |
| R9 | Model extended for export, real PG source refresh/deletion/retry, HTTP CSV comparisons and six source probes. |
| R10 | Separate-DB real old-writer upgrade; no history evidence backfill or new parent facts; migration rollback refuses. |

## Commands, failures and measurements

Every DB command is guarded by
`python3 /tmp/tkada-native-series-20261112/m4a-implementation/run-jb26.py`.
Fixture suites are run serially. One intermediate short suite accidentally
overlapped the live run, and its global export-count assertion saw unrelated
synthetic tenants (4 rather than 1). That run is discarded; the count is now
correctly tenant scoped and final evidence is serial. No FLUSHDB, reinstall or
other task termination occurred.

- Initial behavioral RED: existing export held only member refs rather than the
  captured result. After implementation, new export + original CSV tests pass.
- Live harness setup initially lacked registration in Celery's cached queue map;
  fixed unique-queue registration. Not a product RED.
- First CSV comparison incorrectly compared a full 52-row export to the 50-row
  first page; fixed by following the original snapshot cursor. Not a product bug.
- Six source probes: live recomputation, omitted current grant, omitted BC check,
  legacy reinterpretation, float precision and NULL→0 each produce actual
  AssertionError, then external backup/finally/cmp restore and GREEN.
- `python -m pytest backend/tests/modules/reporting/test_native_exports.py backend/tests/modules/reporting/test_exports.py backend/tests/modules/reporting/test_native_campaign.py -k 'not two_page and not direct_authority and not complete_empty and not account_restore and not ad_evidence and not daily_contract and not demand_rejects' -q --tb=short`: **13 passed, 7 deselected, 1.48s** (includes unchanged same-ID tenant/BC index/isolation check).
- `python /tmp/jb30-rehearse.py`: separate local `jb30_upgrade_test_3430c915`,
  old git source `b0f3b1443e7fedc19f297d078a706d5f8ef50040`, true Alembic
  `actor_authorization_stamp`→head, old process exited before upgrade/current
  consumers; **1 live case passed, 6 deselected, 53.74s**. Script and synthetic
  DB retained locally for inspection; not a production deployment or rollback.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/export_probes.py`:
  six business RED/restored GREEN, logs `/tmp/jb30-probes.log`.
- `python3 .agents/skills/formal-verify-reporting-data/scripts/snapshot.py`:
  128 pass; `--broken-export` gives evidence-only refresh counterexample.
- `bun run --filter frontend build`: main/demo build including tsc passes.

Independent upgraded-DB CSV sample: Campaign 4,236,242 bytes / copied rows
3,591,380 JSON bytes / Worker+HTTP 436.87ms; Adgroup 164,597 / 138,724 / 153.97ms;
Ad 153,315 / 128,071 / 100.30ms. JSON byte numbers here are logical serialization
length, not PG on-disk size. Campaign includes all 52 rows, not just first page.
Frozen vectors/provenance account for most storage; CSV repeats context columns.
No new unbounded benchmark was run. Prior JB29 2k first query 8,964.62ms,
continuation 1,620.69ms, detail 750.36ms, sum(pg_column_size) 11,678,307 bytes;
22 bounded cleanup calls 89.43ms, zero remaining children; actual selection/summary
EXPLAIN ANALYZE 2,066.11/2,362.30ms. Those are inherited one-day synthetic results,
not production capacity. JB27 request counts 2/level (6 total), yield 3/level
(9 total), failed-layer-only retry, independent NOWAIT locks and sync backlog=0
remain valid; new source refresh adds Campaign-only requests. Test private export
queues are deleted after worker exit. Outbox diagnostic records are not a claim
that the production outbox backlog was drained.

## Migration/release boundary and Unknown

`native_frozen_export` follows `native_frozen_query`, adding JSONB contract/map
with empty defaults to ReportExport. It does not rewrite old task payloads or
completed objects. A real release must stop/drain old reporting/projection writers,
apply additive migrations, then start matching API/Workers/export generator;
new and old publishers/readers must not overlap. Rollback needs a reviewed
compatible reader/writer plan preserving direct facts. Automatic downgrade is
blocked; old readers silently discarding direct facts are forbidden.

External platform traffic semantics/total maturity, real production capacity,
production upgrade/rollback, external acceptance and independent whole-MMF review
remain Unknown/not performed. No known JB30 product AC is intentionally left
unimplemented; final review may identify additional defects. Nothing here marks
M4a released or authorizes integration by this implementation worker.

Final live command:
`python -m pytest backend/tests/modules/reporting/test_native_levels.py -k 'two_page_adgroup_worker_and_query and False' -q -s --tb=short`
under the guard: **1 passed, 6 deselected, 54.61s**. Includes actual HTTP
page/detail/create/download rejection with 403 after grant revocation, three
old snapshot details equal CSV, continuation and post-refresh download. Final
first/all-pages/create+duplicate ms: Campaign **860.95/968.39/214.55**, Adgroup
**131.38/132.21/34.11**, Ad **125.14/125.89/32.82**. Final CSV/Worker+HTTP:
Campaign **4,236,242 bytes/544.28ms**, Adgroup **164,553/100.68**,
Ad **153,271/99.48**. Small byte differences from upgrade run reflect frozen
observation metadata, not a monetary mismatch. No fresh 2k export capacity claim.

Changed Python Ruff and diff whitespace checks pass. Narrow strict mypy first
caught a new reused loop-variable type; renamed it without changing behavior.
The two changed typed owners (exports/query_models) then pass; export_tasks
retains its three existing Celery decorator/generic dict diagnostics. No new
diagnostics remain and the pre-existing whole-repository baseline was not rerun.
