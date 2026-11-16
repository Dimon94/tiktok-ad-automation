# 冻结报表缺失数据与证据容量修复合同

状态：本轮修复的实施目标，代码和验收按下列两个阶段串行完成。源基线 `e0e6bdf4`；本计划不授权部署或平台操作。

真实失败：完整模块产物中，5001账户/100k Campaign 的 `query_snapshot.summary` 序列化为970003717字符并触发JSONB限制；并有历史金额被NULL覆盖、默认空对象混入、跨来源缺分母及素材零实例缺失证据丢失。原规模、数值与性能门槛保留。

保留既有QuerySnapshotRow与ReviewSource原件owner，用紧凑引用消除重复，而不靠压缩入库后向HTTP展开巨型数据。新引用是表示合同变更，需要明确版本、迁移实际消费者并验证完整解析；M6/M7、旧daily-backtest输入、真实平台和部署不在本计划内。完整CI证据与修复记录见[模块验证](../../validation/2026-11-16-pr9-module-repair.md)。

## 1. Scope and completion boundary

- Fix the financial/member failures and end-to-end summary size. A compressed 970 MB object that is later expanded for HTTP is not a fix.
- Existing QuerySnapshotRow.frozen_result and ReviewSource.payload.rows remain the complete-source owners. No new fact store/table/provider or API endpoint.
- Summary references an exact frozen selection and bucket, never an inline all-facts registry. Per-row source evidence is stored once and is pageable, including the inner offset of one large row.
- Preserve every selected fact identity, source, scope, raw field/state, Decimal string, real observation time, version, publication identity and digest. No truncation, fabricated observation, fixture reduction or weakened threshold.
- CSV keeps every CURRENT data row kind and row count. NO synthetic SOURCE_OBSERVATION or PUBLICATION rows. Every current row is independently self-contained using one evidence column, metric-local references, and only its own exact publication map. Filtered CSV subsets need no network or omitted row to resolve evidence.
- Keep public QuerySnapshotPublic.publication_map and existing cache prefix unless measurement demonstrates a separate unresolved budget violation. Use the canonical result-contract digest and existing snapshot-current check, not a parallel invalidation mechanism.
- AI references are archive-local, including attribution copies. Preserve the normal 64 KB reading projection, full archive digest, immutable sources and receipts, and delivered-only citation rights. No new completed/revoked task-token rights.
- UI changes migrate actual source displays and consumers. No new dashboard controls or unrelated reporting UX.
- Implementation is complete only when exact business evidence can be resolved through the new references and the real response is bounded. A test that only checks ref existence is insufficient.

## 2. Phase A: financial and membership repairs (independent of wire redesign)

### A1. Legacy parent with no child rows

Owner: row_set._facts_cte, shared by membership/filter/order/summary/row projection.

Select by row existence, not by a metric's truthiness:
1. A direct parent publication is authoritative: use its entire vector, including true NULL/DASH/no returned row, and never resurrect child/legacy amounts.
2. No direct parent publication and child facts exist: use the selected child facts and their own source/contract/publication evidence; a NULL child metric remains NULL.
3. No direct parent publication and no child facts: retain the existing legacy parent numerical columns and NULL flags. Keep CHILD_DERIVED / LEGACY_UNKNOWN; original unknown publication remains unknown. A real legacy parent row is not a fabricated platform observation or NO_RETURNED_ROW merely because the child join is absent.

Do not coalesce a child's NULL metric with stale parent values. Preserve direct authority, targeted publication scope, and contract-mismatch isolation. Reproduce campaign-only spend 1 and the 20k-fact aggregate before fixing.

### A2. No-return authority is not membership

Owner: row_set._facts_cte plus existing member selection.

Synthetic no-return coordinates may establish direct empty authority for an already selected object, but cannot make every directory object a default member. include_empty=False uses actual selected fact membership; include_empty=True and existing explicit ID/search semantics may select empty objects. A published empty direct bucket must still suppress old derived candidates, even when that means the default object is absent. Do not simply remove authority SQL or retain obsolete blanket bans on ad_object access.

### A3. Missing scope survives source-bucket separation

Owners: row_set aggregate/filter/order logic and dimension_query merge/provenance interpretation.

A: Native USD spend10/revenue5; B: selected same-currency member without facts. Preserve independent source/contract buckets, but report known spend10 PARTIAL and local ROAS NULL/MISSING because the requested denominator is incomplete. Do not accept known-bucket 0.5 as the requested complete ratio. Propagate missing-member coverage separately from numeric source buckets; an unobserved member does not become a fake Native record. Do not merge actual incompatible known source amounts. Known field counts and unknown coverage are distinct; expose a coverage-denominator gap without inventing a raw fact count. Apply the rule consistently to SQL filters/order/summary and frozen row metrics.

### A4. Material empty/missing vectors use expected creative coordinates

Owner: material_query.materialize_material_rows.

Both row_missing and directory-empty vector construction use expected level=creative, source_kind=PLATFORM_DIRECT, contract_revision=CREATIVE_OBSERVED_CONTRACT. This is the query's expected source contract, not evidence that a platform row existed. Preserve NULL/no-return semantics and add no observation/time/version. Same VID across accounts must retain the missing contribution; compatible cross-timezone contributions yield MIXED rather than a second anonymous bucket.

### A5. Material zero-instance coverage is not discarded

One contribution collector in the existing material query/freeze owner is called by BOTH frozen_result.freeze_material_results and ai_workbench.review_reporting._material_analysis_rows.

Only INSTANCE values contribute ordinary material money. Additionally retain coverage-only contributions from selected ordinary material members with no instance, including independent EUR missing buckets and different-VID missing members. Do not fabricate an INSTANCE. Do not sum unmapped/composite/residual monetary values by copying every report.metric_buckets wholesale. Ordinary known money plus appropriate unknown coverage must remain partial/missing in summary exactly as on the row.

### A6. Deterministic exact evidence

Owners: row_set observation ordering and dimension_query evidence normalization.

Canonicalize using exact selected row identity, bucket coordinates, original source-fact identity/date, and publication identity/digest. Preserve child IDs and selected parent scope. Never deduplicate distinct targeted observations merely because their parent ID, time or amount matches. Exact publication stamps may be interned; distinct facts must remain individually recoverable. Repeat equivalent account reads must produce the same evidence order and digest independent of the planner's unordered jsonb_agg order.

### A7. Explicit existing no-spend cost zero

M4a spec line 215: directory-only revenue is MISSING/NO_RETURNED_ROW, while the already defined no-spend cost-zero behavior remains. Audit both row_set._member_buckets and dimension_query.empty_values_for; current blanket parent NULL is not an acceptable replacement.

Use the existing selected coverage owner (including levels.state_sql direct/derived precedence), over the full requested date range, and distinguish an actually absent selected fact from a returned row with a NULL metric. Only an empty object satisfying the pre-existing COMPLETE/no-fact no-spend rule gets spend=0. Incomplete/missing coverage, returned NULL/DASH/ABSENT and unknown/uninterpretable contract do not become zero. Do not extend the zero rule to new revenue money. Keep other pre-existing non-revenue additive zero behavior only where the current catalog/accepted rule defines it; derived ratios retain their denominator rules. Native direct empty authority must not source the zero from an old derived value. New tests distinguish absent row, actual zero, returned NULL, incomplete scope and unknown contract.

## 3. Phase B: compact summary and complete-source contract

### 3.1 Logical shape

Existing bad shape repeats the entire observations/buckets/local_ranges arrays in every metric's provenance. New summary carries scalar/statistical provenance and ONE bucket-level EvidenceRef. It never carries source_evidence records.

Representative new summary bucket (illustrative values):

```json
{
  "currency": "USD", "timezone": "MIXED",
  "level": "campaign", "source_kind": "CHILD_DERIVED",
  "contract_revision": "LEGACY_UNKNOWN",
  "values": {"spend": "100000", "roas_d0": null},
  "availability": {"spend": "PARTIAL", "roas_d0": "MISSING"},
  "reasons": {"roas_d0": "MISSING_DENOMINATOR"},
  "provenance": {
    "spend": {"known_count": 100000, "missing_count": 0, "field_state_counts": {"LEGACY_UNKNOWN": 100000}},
    "roas_d0": {"denominator_known_count": 100000, "denominator_missing_count": 0, "denominator_coverage_incomplete": true}
  },
  "evidence_ref": {
    "owner": "QUERY_SNAPSHOT_ROWS", "owner_id": "<snapshot UUID>",
    "row_key": null,
    "bucket": {"level": "campaign", "source_kind": "CHILD_DERIVED", "contract_revision": "LEGACY_UNKNOWN", "currency": "USD"},
    "scope_digest": "<sha256>", "record_count": 100000,
    "evidence_digest": "<sha256>"
  }
}
```

The existing explicit metric reasons/formulas remain canonical; implementation should reuse catalog reason values rather than introduce synonyms solely from this example. Keep existing level/source/contract, metric_contract, formula, field_state_counts, known/missing and denominator counts. Replace repeated full observation/publication/local-range arrays with an evidence reference and bounded time-range statistics (requested dates and actual min/max observation/local-date bounds). Actual per-account ranges remain in the complete records. Unknown denominator coverage is represented separately from the number of observed missing fields.

EvidenceRef fields:
- owner: QUERY_SNAPSHOT_ROWS for browser; FROZEN_REPORT_ROWS for an archive-local copied report; ROW_EVIDENCE for a self-contained CSV row.
- owner_id: actual snapshot/report ID; CSV uses stable row identity within its export, with the export ID in the existing envelope.
- row_key: exact frozen row, or null for the entire selected rowset.
- bucket: exact level/source_kind/contract_revision/currency partition. Current bucket_key does not split compatible cross-account timezones; retain MIXED and actual source ranges. Include any already enforced semantic coordinates rather than relaxing them.
- scope_digest: selected identity/selection-scope digest, independent of display ordering.
- record_count and evidence_digest: count/digest of the exact resolved complete records.

Full source records live once under source_evidence in each existing frozen row. Reuse the selected record structure: advertiser_id, source remote_id, selected row/parent scope, level, source_kind, contract_revision, local_day, timezone, currency, returned, fields, fact (Decimal strings/null), publication. Do not discard open source fields from existing evidence. Do not generate a new observed time or treat a current directory label as historical source evidence. A bucket reference selects the corresponding records; it does not repeat their indices or identities in summary.

Row metric provenance no longer repeats its full records for each metric. Public metrics rows expose the ref plus compact provenance; full records are returned only through evidence pagination, never reconstructed from current facts. Single-object platform ratio display remains unchanged. AdsTable/FrozenReport source expansion resolves the same frozen evidence rather than embedding another platform_roas_observations JSON copy.

### 3.2 Digest and atomicity

Use the existing canonical JSON/digest conventions, with Decimal as strings, sorted object keys and a deterministic record ordering. Hash the complete logical records, including exact fact identity, fields/state/value, source scope, real publication/version/id/digest; do not hash only numeric totals or only a reference.

Per-row evidence digest and summary bucket digest are computed from the same frozen records. A deterministic length-delimited stream of canonical record bytes or fixed-size record hashes avoids ambiguous concatenation. The final bucket digest includes the bucket and selection scope. Process rows in deterministic row-key order (pagination stable_sequence remains the user-facing sort); do not collect all raw records into a Python summary list. Amount aggregation remains in SQL.

Build members, totals and source records in the existing repeatable-read construction transaction. Bind/finalize refs only after full frozen rows exist, before the result becomes readable. Any failure rolls back the whole result. Missing/mismatched referenced data is an explicit unavailable result, never a live-source fallback.

### 3.3 SQL changes and all known call sites

| Caller | Required behavior |
| --- | --- |
| row_set.insert_snapshot_rows -> _summary_select | Overall summary returns amount/null/coverage and compact evidence counts/min/max; no jsonb_agg of all raw observations into summary. Existing per-row frozen_projection retains full selected records for freezing. |
| queries._build_snapshot -> _summary | Compose compact vectors from SQL stats; freeze rows; finalize exact owner refs and digests in same RR. No vector_provenance over the whole population. |
| dimension_query.vector_provenance/report_provenance | Preserve all business semantics/counts using per-row selected records, store those records once, and produce compact metric provenance/reference. Canonical ordering is shared. |
| review_reporting.freeze_report -> _summary_select (overall) | Compact stats, no whole-population records. Selected-fact observed max must still enforce cutoff, including newer targeted facts with older account watermarks. |
| review_reporting.freeze_report -> _summary_select (per_row), _analysis_rows -> _summary | Preserve full row records once in archive rows; compact metrics reference archive-local records. Do not invoke an overall browser-snapshot reference constructor. |
| review_reporting._material_analysis_rows | Use the shared material money/unknown-coverage collector and retain existing instance/source evidence in original archive rows. |
| queries.freeze_review_page -> attribution.build_attribution_pack | Copy ALL required full frozen rows, including complete evidence, before short snapshot expiry; rebind refs to the copied report-local rowset. Never serialize the compact public page as if it were complete archived evidence. |
| review_timeline / _daily_backtest_inputs | Outside this change: existing rule_backtest_inputs archive and legacy campaign_report_day semantics remain. No Native summary routing or new analytical meaning. |

The per-row and overall _summary_select/_summary contracts must be explicit (e.g. full-row evidence versus compact stats) rather than relying on an absent optional key that silently drops proof.

## 4. Existing-route evidence pagination and authorization

### Browser

Extend existing GET /ads with view=metrics|evidence (default metrics), evidence_row_key and evidence_cursor. Existing limit remains 1..100. evidence mode requires snapshot_id and uses its already frozen filters/selected membership; it cannot start a new query or expand the scope. Return a typed evidence_page in AdsQueryPage for this mode, with records, next_cursor, total_records, scope_digest and evidence_digest. Normal item pagination remains unchanged.

The evidence cursor binds snapshot ID, tenant/BC/actor, selected scope and bucket/digest, exact frozen row position and INNER record offset. A whole-summary request walks the selected rows; a row request stays in that row. This makes one large account row pageable, not merely a list of large rows. Fetch the needed frozen row evidence slice through the existing JSONB row owner; do not load the entire row's observations into the public response to slice it afterward. No record is silently truncated. Evidence pagination uses the existing snapshot access/authorization checks on every request.

Existing get_ad_detail is not the sole resolver: it only materializes campaign/adgroup/ad reporting and first locates current directory state. Account/drama or deleted current directory objects must still resolve their immutable reporting evidence through the authenticated snapshot route. Detail returns compact metrics/reference and its source view uses the same resolver.

Keep QuerySnapshotPublic.publication_map unchanged initially. Measure its actual bytes separately. If it becomes a separate capacity failure, report evidence and obtain root scope review before replacing that public field.

### AI archive

Create report ID before building its refs. FROZEN_REPORT_ROWS references resolve relative to the current ReviewSource payload, including nested attribution report IDs; they never name an ephemeral QuerySnapshot. Source ID/digest in the existing read envelope anchors the relative reference without a self-referential archive digest.

Extend existing POST /{task_id}/evidence/read body with view=reading|provenance (default reading), report_id/row_key/bucket selectors as needed, and evidence_cursor. Ordinary rows/offset/limit behavior and 64 KB projected read budget remain. Provenance mode traverses frozen records with inner offset, returns complete records with a continuation cursor, and is subject to the existing response budget (reduce page count, never truncate one record).

Keep source.payload immutable and validate its existing digest. A successful delivered page, not an undisclosed archive object, grants citation rights. Receipt identity includes source ID/digest, contract, view, selectors and cursor/range, so different modes cannot collide or gain rights through an older receipt. Enforce existing task_identity and completed/revoked-token boundaries. Existing authorized reanalysis/archive access remains the only alternative for old completed tasks; do not add new token rights as a convenience.

review_projection keeps the normal compact reading behavior but preserves the explicit evidence locator instead of only provenance_details_archived=true. read_for_task in provenance mode does not run the stripping projection. Nested attribution refs are rebound and resolved against copied rows. Archive digest/source catalog/reanalysis consistency checks continue to apply.

## 5. CSV: no new data records, no external resolver

Keep existing _export_rows row kinds and data row counts exactly, including existing material INSTANCE/control/reconciliation behavior. Do not introduce SOURCE_OBSERVATION or PUBLICATION data rows.

For each CURRENT CSV data row:
1. Add a source_evidence column containing that row's complete evidence once, canonically encoded with Decimal strings and all original source fields.
2. metric_vector contains compact per-metric provenance with ROW_EVIDENCE local refs/digests; the ref resolves entirely within this same CSV row.
3. publication_map contains only this row's exact referenced source publications and the coverage/control publication coordinates genuinely used to explain this row. Select from the frozen owner and exact ref set, not current report_sync_state or advertiser-wide guesses. Deduplicate exact publications without erasing targeted scope/version/digest.
4. Existing material observations can serve as the local source collection where already canonical; avoid storing the identical records in both old observations and a new column. Explicitly map the row-local ref to its actual evidence column under the new contract. Preserve all existing meaningful CSV evidence fields; remove only duplicate representation with the revision change and reader migration.
5. export ID/result contract/filter scope/digests identify the immutable export. Export creation copies full evidence from original frozen rows; do not copy public compact projections. Later CSV generation does not require the original QuerySnapshot, network or directory.

A filtered CSV containing only a subset of its current rows remains independently interpretable: no shared global registry, no reference to an omitted row, no network lookup. Known numerical columns, NULL-as-empty, true zero, exact Decimal precision, signs and row counts remain unchanged. CSV parsing changes are intentional and versioned; readers that previously searched per-metric observations must resolve the local evidence column instead.

## 6. Contract revisions and existing invalidation

| Contract | Proposed new value | Reason |
| --- | --- | --- |
| RESULT_CONTRACT.revision | selected-report-result-v2 | Compact provenance/reference and frozen evidence retrieval |
| MATERIAL_RESULT_CONTRACT.revision | material-instance-result-v2 | Shared result/evidence envelope and corrected missing coverage |
| MATERIAL_QUERY_CONTRACT | 4 | Reject incompatible material snapshots/selection consumers |
| AI projection_contract | financial-review-v2 | Archive-local full source rows and references |

Keep actual metric semantics revisions (NATIVE_CONTRACT / CREATIVE_OBSERVED_CONTRACT) unchanged; this is result representation, not a new revenue definition. Preserve outer on-demand-v2 task authorization contract unless an actual caller dependency requires change; use explicit financial report revision checking for the changed payload.

query_reuse already includes the canonical RESULT_CONTRACT digest (material uses MATERIAL_QUERY_CONTRACT), and snapshot_contract_current compares current result contract. Reuse those checks. Audit page_cache hit flow to ensure old cached pages cannot bypass the canonical current-contract gate; route that check through the existing owner if missing. Do not add a cache-prefix version as a second invalidation owner without demonstrated need.

Old explicit snapshots: current read_snapshot returns 409 query_snapshot_contract_changed, including detail/evidence/selection/export entrypoints. Old export generation/regeneration: existing report_export_contract_incompatible_reexport 409. Completed immutable CSV artifacts remain downloadable under the existing authorization and expiry checks; serving their original bytes does not reinterpret or regenerate an old contract. New queries may create new results; no legacy decoding/live fallback. AI changed financial archive reads/reanalysis reject incompatible report revisions explicitly, without rewriting immutable sources or receipts. Unrelated legacy daily-backtest archive paths retain their existing contract and meaning.

## 7. Minimal owner and consumer map

| Area | Files / impact |
| --- | --- |
| Shared source/amount selection | reporting/row_set.py, dimension_query.py; maintain one SQL selection owner and Decimal semantics |
| Material contribution collection | reporting/material_query.py and callers in frozen_result.py / ai_workbench/review_reporting.py; one unknown-coverage collector |
| Snapshot/ref/DTO lifecycle | reporting/frozen_result.py, queries.py, schemas.py, query_models.py; no new persistence table |
| Existing HTTP/source UI | reporting/api.py, detail.py; generated client; AdsTable.tsx and details/FrozenReport.tsx plus their actual data hooks/callers. AdsSummary only consumes compact values and any required type migration; no new dashboard controls. |
| Cache/reuse | query_reuse.py/page_cache.py: audit and reuse canonical contract gate; avoid unrelated changes when already sufficient |
| CSV | reporting/exports.py: full frozen source copying, per-current-row evidence/local refs and exact publication selection |
| AI | review_reporting.py, review_reading.py, review_projection.py, runtime.py request DTO; review_reanalysis.py contract gate; queries.freeze_review_page/attribution caller for copied-row binding. review_data/preparation/store audited as existing owner/envelope, only modify where required to carry new revision/ref without rewriting sources. |

Actual public users affected: generated SDK consumers of MetricProvenance/ReportRow, browser source panels, JSON/CSV evidence consumers and AI evidence read/reanalysis. Numeric display/selection semantics change only for Phase A bug fixes. Deep evidence shifts from repeated eager fields to explicit frozen resolution; source UI uses existing expansion flow. External clients must honor new result revision; do not claim invisible compatibility.

## 8. Test map and acceptance evidence

This contract is the implementation target; test results belong in the validation record. Reuse the already committed fixtures at e0e6bdf4, including test_creative_queries and the canonical claiming/historical boundaries.

### Phase A real business regressions

- test_account_read_models.py: legacy parent-only cost1 / 20k costs; child-present true NULL stays NULL; direct/no-return and targeted source precedence retained.
- test_complete_empty.py: absent versus returned NULL/DASH/true zero; include_empty=False ad membership; known+missing cross-source denominator must be NULL; retain precise spend0 while revenue no-return is missing.
- test_material_query.py, test_creative_queries.py, test_material_metrics.py: same VID/missing account, different VID missing, zero-instance all missing, separate EUR, MIXED timezone. Assert row and summary and original raw observation absence.
- test_material_instance_tracer.py: keep ordinary mapped INSTANCE totals; no unmapped/composite/residual money leaks.
- test_account_read_models.py: repeated logically equivalent query evidence ordering/digest stable.
- test_entity_metrics.py, test_drama_materialization.py, test_query_page_cost.py: replace stale physical-table/display-copy prohibitions with proper current source/row semantics and bounded EXPLAIN guards, not weaker numeric expectations.

### Capacity, API and R9 proof

- test_account_read_scale.py: original 5,001 account / 100k Campaign input, no reduction. Assert exact100000 spend, selected count, first50 rows. Measure DB summary/frozen-source bytes AND actual serialized HTTP response bytes AND peak construction behavior; summary size must not grow with per-fact observations. Keep thresholds justified by before/after measurements, not raised to pass.
- Single-large-row test: one account/row owns many facts; evidence cursor traverses inner offsets without an oversized first response, duplicates or gaps.
- Whole-population resolution: iterate ALL evidence pages and compare complete exact records and canonical digest/count against frozen selected sources, including differing targeted/full publications, raw fields, precise Decimals and legacy unknowns. Not just refexists.
- test_native_snapshot.py + test_targeted_provenance.py: all former observations/buckets/time/version/scope assertions now resolve the same complete records; freeze, later mutate publisher values and directory, read every evidence page/row/detail, and require unchanged old evidence. Wrong digest/missing frozen payload fails; no live recovery.
- test_native_exports.py: no data-row count/kind changes; each individual CSV row resolves locally even after other CSV rows are removed and snapshot expires; exact full source records, local publication map and raw ratio precision preserved. Old export revision 409.
- test_query_reuse.py + test_page_cache.py: old contract cache/reuse cannot escape current-contract checks; new result reuses according to canonical digest, without extra invalidation schemes.
- test_material_query_lifecycle.py: new material contract, old snapshot exit, expiration and atomic rollback retained.
- Permission matrix: tenant/BC/actor mismatch, revoked account grant, wrong cursor scope/bucket, expired snapshot; evidence mode has no broader access than ordinary snapshot. Keep current error/disclosure boundaries.

### AI consumers and independent archive lifetime

- test_review_preparation_performance.py and test_review_data.py: direct freeze path remains without browser QuerySnapshot; compact overall stats, full per-row evidence archived once; selected targeted observed-after-cutoff still rejected.
- test_review_reading.py: normal projected reading stays <=64KB; provenance pages preserve raw records and exact digest, complete large-row traversal, receipt modes/selectors/cursors do not collide, only delivered pages are citable; existing completed/revoked task-token refusals unchanged.
- test_attribution_evidence.py: archive copied rows resolve after originating QuerySnapshot expiration/deletion; references are report-local and preserve the original scope.
- test_review_reanalysis.py: archive-local digest/revision/source completeness retained; old incompatible financial revision explicit failure; sources/receipts not rewritten.
- test_review_timeline.py: unchanged original rule_backtest_inputs behavior; do not claim Native coverage or fix legacy analytical semantics.

Run order after lease: first smallest Phase A RED nodes, implement and pass; then new bounded-reference RED cases and consumer migration; run relevant existing proof assertions on resolved evidence; finally original capacity case and actual UI source view. Do not rerun unaffected Native creation/recovery or platform acceptance suites without a specific changed dependency.

Guarded command template, from backend with explicit tests/... paths:

```bash
PG_BIN_DIR=/usr/local/opt/postgresql@17/bin python3 /tmp/tkada-pr9-reporting-clean-20261116/run-test.py python -m pytest -q --tb=short --show-capture=no <explicit selected test files/nodes above>
```

Syntax/type/generated-client/build checks follow actual changed files and package scripts. Record source HEAD, exact commands, RED failure class, final PASS and unresolved limits. A new failing acceptance supersedes old passing evidence. Integration and final CI are separate from these local checks; deployment and real platform operations remain outside this change.
