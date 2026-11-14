# M4a whole-MMF — Spec review R1/3

**BLOCK — P0: 0, P1: 1, P2: 2, P3: 0.** Independent Spec axis only; no M4a release/integration approval.

Tree: `/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb24-native-growth-m4a`

Base: `b0f3b1443e7fedc19f297d078a706d5f8ef50040`

Head: `573ebc8fc2f50d212856c9d8ec23a29645ae3b59`

Verified HEAD and merge-base equal the supplied refs; bundle matches; all 16 commits and 80 paths match the supplied lists. Exact three-dot comparison retained. Only pre-existing untracked `.venv`/`node_modules` observed. Used the requested code-review skill, Codex gpt-6.1-sol/high; no delegation.

## P1 — New frozen provenance assigns an old whole-account observation to newly replaced subset facts

**Location:** `backend/app/modules/reporting/row_set.py:314–359`, especially **line 359**; producer `backend/app/modules/reporting/publish.py:484–513`.

**Spec:** JB-24.md:56 (R9): “快照固定实际指标结果、所选来源与水位”; JB-24.md:194 requires “Ad既有campaign_ids定向任务继续发布其精确范围，只满足原冻结需求，不推进整账户日COMPLETE”; JB-29.md:18 requires “每行冻结本次实际 MetricVector、逐指标 provenance … 实际 scope 和逐日水位”. Preserving the whole-account completeness stamp does not authorize attaching it as the observation of a different, subsequently replaced fact.

**Trigger and observable impact:** A successful full Ad publication is followed by an authorized `campaign_ids` publication for one Campaign. The latter replaces its AdReportDay rows but skips the entire new evidence-stamping block because `campaign_ids` is nonempty. The selected-query SQL nevertheless joins each row to the account/day `report_sync_state`, and unconditionally serializes `to_jsonb(s)` as its publication. It never consumes the fact's own observation/version. New table rows, snapshot detail and CSV therefore freeze a new amount together with the old whole-account observation time/version/digest. “Frozen” consistency between outputs preserves this false attribution rather than correcting it. This breaks the new public provenance contract on an expressly retained production entrypoint.

**Concrete counterexample for root to run serially:**

1. Through the existing authorized Ad publisher/Worker, publish account A/day D at `t0`: Campaign C1/Ad a1 has spend 10, total 20; C2/Ad a2 has spend 90, total 90. Retain the resulting `ad_day` publication version V and digest H.
2. At `t1 > t0`, run the existing precise `campaign_ids=(C1,)` task with a1 spend 50, total 100. It succeeds and changes a1; its state remains at t0/V/H, correctly avoiding a claim that the entire account was refreshed.
3. Make a fresh Ad query for a1 and create its CSV. The new amount is 100, but `provenance.revenue_total.buckets[0].observed_at` and `display.report_observed_at` are t0, version V; snapshot publication map still carries H from the earlier payload. The new fact evidence contains neither its t1 observation nor its own publication version. The result claims a1=100 under an observation that contained a1=20.
4. Repeat with no earlier full Ad publication: a newly evidenced native fact is instead classified `LEGACY_UNKNOWN` because `fact_coordinates()` reads the absent account-level active contract rather than the row contract.

**Same-class diff audit:** This account-state-only stamp is shared by ordinary Ad queries and `contribution` queries in Campaign/Adgroup/account/drama views. Before any direct parent publication, parent CHILD_DERIVED candidates likewise attach `derived_s` (the old Ad account stamp) after targeted Ad-derived rebuilds. `freeze_results()` persists those selected observations; both supplied-snapshot detail and frozen CSV inherit them. Full Campaign/Adgroup publications are not this counterexample: their producer updates facts and their own bucket state together. The fix must preserve precise-scope observations without falsely advancing account completeness.

**Exact-base check:** Base `publish.py` already excludes targeted writes from `_set_state`; that intentional behavior is not this finding. Base `row_set.py` has no `fact_coordinates`, evidence/publication observation JSON or frozen result contract; base AdReportDay has no evidence column. This diff newly creates and exports the false fact-to-observation attribution. The finding is not the pre-existing possibility of targeted/full writers interacting, nor a request to advance the whole-account state.

**Verification:** Personally executed an in-memory extraction of the exact `_facts_cte`/`fact_coordinates` source, without importing the app or opening PG. Both Ad and parent-contribution SQL emit account-state-only publication coordinates. Producer branches and freeze/detail/export consumers were traced statically. The PG/HTTP counterexample above was not executed in this review; root must validate it serially. Its expected path follows the preserved targeted-publish branch and newly added serializer directly.

## P2 findings (nonblocking)

1. `dimension_query.py:1277`: legacy NULL evidence becomes ABSENT, not LEGACY_UNKNOWN (JB-24:49,190; JB-25:19–20). Counterexample: legacy Ad revenue NULL, evidence NULL, selected observation fields `{}` → field-state count ABSENT=1 in row/CSV. Exact-source isolated formatter execution reproduced this; base had no formatter.
2. `dimension_query.py:1503`: returned Ad rows with unrequested platform ratios report NO_RETURNED_ROW (JB-24:49,190). Counterexample: a current-contract Ad row has spend/revenue evidence but no requested platform ratio; table/detail/CSV say NO_RETURNED_ROW. `test_native_levels.py:726` expects NOT_REQUESTED, which this path cannot produce. Base had no such display contract.

## Spec gap

JB-24:122 “本次新增采集平台total/D0 ROAS” can mean all three daily levels, or the new parent readers while retaining Ad's request. Line190 explicitly distinguishes unrequested ratios. Current Ad requests omit both. Clarify intended Ad collection; not an additional finding.

## Verification and boundaries

Personally: ref/list verification, production-source tracing, SQL extraction, legacy formatter reproduction, diff whitespace check. Initial whole-file AST parsing failed under system Python3.11 on existing Python3.12+ generic syntax; fragment extraction succeeded. No product writes, mutation probes, DB fixtures, platform/Linear/production/PR/Git mutations.

Inherited only: root's native_exports+exports12, snapshot+reuse19, scope+dimension79/scope63; SDK/PG/Redis/HTTP/browser/upgrade evidence in JB25–30 validation documents and formal artifacts. Not rerun or treated as independent proof. Bounded models do not prove the whole system. Real platform/production acceptance remains Unknown and adds no blocker. No old mypy/410 failure counted as regression.
