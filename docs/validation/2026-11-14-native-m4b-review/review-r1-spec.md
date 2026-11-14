# M4b whole-scope independent review — Spec axis — R1 / max 3

Verdict: **PASS**. Counts: **P0=0, P1=0, P2=2, P3=0**. No confirmed unresolved P0/P1. This is a Spec-axis verdict, not whole-MMF acceptance, release approval, or a Standards review.

Reviewer: Codex gpt-6.1-sol/high, performed directly with no child agents. Applied `/Users/dimon/.agents/skills/code-review/SKILL.md`; user scope overrides its two-agent workflow. Product source remained read-only.

## Fixed scope and provenance

- Sole tree: `/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb33-native-material-m4b`
- Branch: `feat/jb33-native-material-m4b`
- Baseline: `1c987867971e8340ff793597b3c42c6acbb6fecc`
- Head: `8d3629b9864389d0e8a6ff4b1b8ab53bd43d5c3e`
- Exact diff: `git diff 1c987867971e8340ff793597b3c42c6acbb6fecc...8d3629b9864389d0e8a6ff4b1b8ab53bd43d5c3e`
- Personally verified HEAD, branch, merge-base=baseline, all 16 commit-list entries and all 50 diff paths against `review-r1-bundle.json`, using Python assertions over read-only git output. No alternate baseline.
- Authoritative specs: external published JB-33.md and JB-34.md through JB-38.md in this directory; source-notes document supplies context, not a replacement Spec. Implementation/root stage acceptance is inherited evidence, not this independent verdict.

## Findings (all P2/P3 text below totals under 200 English words)

1. **P2 — Residual CSV coordinates disappear.** JB-33:256: “差额保留广告组范围与符号”; :66 requires currency/timezone. Trigger: ADGROUP_RESIDUAL=-7 USD. `backend/app/modules/reporting/material_query.py:647` empties buckets; `exports.py:513` emits them. Original-source formatter counterexample confirmed empty currency/timezone/spend columns; -7 survives only nested observations. Baseline exported original residual buckets. Spreadsheet reconciliation therefore loses ordinary numeric coordinates, but recoverable JSON limits severity. Residual-only details also bypass rendering at `frontend/src/features/ads/details/MaterialsTab.tsx:47`. Same-class check: intentionally empty incomparable parent differences are distinct.

2. **P2 — Instance navigation absent.** JB-33:80: “从实例导航到同范围内的父 Ad 或 creative 详情”; :365 requires links or unavailable reasons. Trigger: open a resolved instance with existing parent/creative. `frontend/src/features/ads/details/MaterialsTab.tsx:517` renders IDs and JSON without links or current-existence reasons; navigation stops there. Baseline had no instances: incomplete new behavior. Parent controls share the omission.

## Independent source trace / coverage

Paths below are relative to the sole tree. These are source conclusions, not claims of fresh browser/PG/Redis execution.

| Spec / behavior | Independently traced path and assessment |
|---|---|
| R1 identity / authorization | `filters.authorized_grants`, `creative_query.filtered_ctes`, `freeze_instance_tracer`, `query_models.read_snapshot`, `exports._scope_check`: tenant/account authorization and actor-owned snapshots/exports; source connection ambiguity/mismatch is explicit special-row evidence. See interpretation limit below. |
| R2 direct economics | `jobs._run` -> `publish_creative_day` -> creative projector -> material query vectors. Instance inputs are creative facts; independent parent control is materialized separately. No parent monetary backfill found. |
| R3 original fact key / five row types | Publisher deduplicates tenant/advertiser/day/creative observations; projector keeps one economic fact, tracer deduplicates advertiser/creative/day within identity groups. Ordinary, unmapped and carousel buckets remain distinct. Export emits INSTANCE/PARENT_CONTROL/UNMAPPED_CREATIVE/COMPOSITE/RECONCILIATION_DIFFERENCE. No new additive publication/contract copies found. |
| R4 five states / legacy | `creative_metric_evidence`, nullable evidence migration/store/projector, tracer observations and field counts: VALUE/NULL/DASH/ABSENT retained, absent historical evidence explicitly LEGACY_UNKNOWN. No historical backfill. |
| R6 metrics / denominators | `metric_vector`, `aggregate_metrics`, strict-denominator material calls, `semantics_for`: Decimal and ratio-of-sums; missing denominator suppresses ratio, partial numerator uses complete denominator. Currency/source-contract buckets remain separate; same-currency mixed account timezones become MIXED. Creative Native Growth window/traffic/maturity stays unconfirmed. |
| R7 parent controls / comparisons | Tracer creates fresh parent filters with only advertiser, parent ID and original date window; material subset filters do not constrain FULL_PARENT_AD. `reconcile_parent` reads complete parent creative scope independently, checks selection, coverage, observation, currency/day and contract conditions. Unknown creative native semantics reject native differences, without redefining the independent parent control. Original adgroup residual presentation has finding 1. |
| R8 directory / references | Projector/tracer retain identity evidence, assets, creative IDs, missing MID and ambiguity reasons in frozen payload. Navigation omission is finding 2. |
| T1 / R5 frozen page-detail-summary | `queries._build_snapshot` -> `freeze_material_results` captures rows, instances, controls, reconciliations and SELECTED_MATERIALS summary. RR transaction covers capture; page uses `frozen_row`; material details render row payload; export copies frozen rows. No live material reaggregation fallback remains. |
| T2 / T3 failure/requery | RR builder rollback prevents partial snapshot publication. Page-cache failure marker is lease-token guarded; frontend clears errored results, exposes requery and forces a new request. No persistent half-result path found by source tracing. |
| T4 / T12 15-minute TTL / contract | Original snapshot expiry is 15 minutes. `read_snapshot` and reuse owner reject old result/material contracts and expired snapshots; requery can build a fresh snapshot. No source TTL extension found. |
| T5 / T8 / R9 creation-retry | Existing scoped idempotency lookup, task row lock and FAILED outbox rearm retain original export/payload/expiry; permissions checked on retry. |
| T6 / T7 / R9 publication-failure | Original worker locks export for generation and handles failure without overwriting COMPLETE. Generation checks current authority before/after object put; failures delete deterministic object key. DB completion follows whole object persistence. |
| T9 / T10 / T11 download-expiry | Original 24-hour export owner retained. Download checks current scope, status, exact object key and expiry; expiry/cleanup deletes task object; queued/running generation rechecks expiry before publishing. Completed old-contract objects are downloaded as original bytes; incompatible pending exports fail before new formatting. |
| R10 CSV | Frozen payload exclusively drives CSV, explicit row kinds separate parent controls and differences from material totals; parent controls deduplicated by key, formula-injection protection retained. Finding 1 limits residual-column parity. |

## Spec interpretation gaps (not findings)

- JB-33:57/:254 require BC/connection authorization/source identity, while original economic facts are tenant/advertiser/creative/day and BC/connection must not create additive copies. For the **same advertiser currently authorized in two BCs**, wording permits (A) showing the shared account fact in a clearly marked SOURCE_SCOPE_MISMATCH special bucket, or (B) excluding all facts observed via another BC. Current code chooses A (`material_query.py:429` onward; explicit test at `test_material_instance_tracer.py:663` onward). It does not fabricate an ordinary exact instance for mismatch. Spec should state whether A's retained evidence/amount is allowed. This is not evidence of an unauthorized-account leak, and is not counted as one.
- JB-33's typed `get_material_instances`/cursor interface versus its workspace frozen-payload rendering description permits server instance paging or client paging over a captured row. Current implementation uses the latter. I do not classify lack of a separate endpoint as a product failure.

## Personally run versus inherited checks

Personally run:

1. Read-only git/bundle assertions described above; inspected exact three-dot changes and relevant original owners plus baseline paths for reported deviations.
2. Targeted Nowledge memory search `JB-33 M4b native material`: no results. Local memory keyword lookup also found no relevant entry; no historical decision used as authority.
3. Pure in-memory counterexample: parsed current source AST, compiled the unchanged original residual branch and `_decimal`, `csv_safe_text`, `_export_rows`, `_csv_rows`; supplied a synthetic residual payload and actual current result contract/metric catalog. Ran through the external guarded Python runner with bytecode disabled. Confirmed empty CSV amount/currency/timezone and retained nested -7. No database, Redis, Worker, object-store or platform calls. This verifies formatting only, not complete runtime behavior. Initial harness attempts failed on missing AST line metadata and import ordering; corrected harness loaded normal app model registration first. Final execution exited 0. No source edited.

Inherited, **not personally rerun**:

- JB34–JB38 validation records and JB38 T1–T12/R1–R10 map; root stage results in bundle (JB34 22+3, JB35 21+6, JB36 13, JB37 12, JB38 21; bounded-model counts as recorded).
- Browser/HTTP/PGRedis, SDK transport-double/original Worker, local object fallback, fault injection, concurrent worker and revoked-permission suites. Their stage acceptance does not substitute for this review.
- Source-mutation probes and bounded models. None run here; finite assumptions are not whole-system or unbounded proofs.

## Verification limits and disposition

No shared PGRedis fixture suite, mutation probe, server or migration was run. No real platform/production API, remote S3, account operation, product edit, Spec edit, commit, Linear write, push, PR, merge or deploy. Dynamic behavior outside the pure formatter counterexample remains inherited, not independently revalidated this round. Synthetic authentication fixtures do not establish production authentication behavior. Remote S3 failures, creative Native Growth support/D0/traffic/maturity and production capacity remain accepted **Unknown**; they are not invented findings.

No unresolved P0/P1 was established on the Spec axis. PASS uses the supplied severity gate; the two P2s remain follow-up items, not assertions of fully complete requested UX. This report neither closes tickets nor declares M4b accepted or released. Standards and overall MMF acceptance remain with the coordinating reviewer.

## Fixed commit list

- `8d3629b9 docs(reporting): record root acceptance of material export lifecycle`
- `ff62f283 test(reporting): bind material export lifecycle formal evidence`
- `e400e45e fix(reporting): complete frozen material export lifecycle`
- `6bcf1add docs(reporting): record root acceptance of material query lifecycle`
- `708cc31a test(reporting): bind material lifecycle invariants to storage probes`
- `96d041d2 fix(reporting): close material query failure and requery lifecycle`
- `b62be2ca docs(reporting): record root acceptance of material reconciliation`
- `c6282cd3 test(reporting): export all material row kinds through browser`
- `03ccd781 test(reporting): bind material metric invariants to source probes`
- `981579bc feat(reporting): freeze material metrics and scoped reconciliation`
- `8a743863 test(reporting): align creative scheduling with direct parent facts`
- `6454a666 test(formal): bind material identity and five-state rejection`
- `4c07e42a feat(reporting): freeze material identities and creative field evidence`
- `cf20640c fix(reporting): recognize the creative observed contract`
- `033b5afc test(formal): bind material tracer invariants to real source probes`
- `54d55fd6 feat(reporting): freeze material instance tracer and parent control`
