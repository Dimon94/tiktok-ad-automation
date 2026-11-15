# wholeM5a Standards R1 — PASS

Round 1/max 3. Independent Codex gpt-6.1-sol/high; no subagents. Standards axis only.

**Counts: P0=0, P1=0, P2=1, P3=0. No unresolved P0/P1.** This is not wholeM5a release, integration, deployment or real-provider approval.

## P2 finding (nonblocking)

**S1 — Accepted status tasks keep waking without an observation plan.**

Location: `backend/app/modules/ad_management/execution.py:647–655,749–781`; prerequisite: `:695–698`. With `ADS_SYNC_ENABLED=True` and no enabled `AdSyncBc`, refresh enqueues no directory reads. MCP-only BCs can have this configuration (`reporting/planner.py:114–129`). Accepted status items now remain REFRESH_PENDING; subsequent wakes repeat the no-op refresh, schedule another wake and keep the task RUNNING. Native/ordinary status tasks and restores share this path.

Violated standard: `docs/superpowers/specs/2026-09-30-ads-management-reporting-design.md:213` separates request acceptance from background observation; `AGENTS.md` requires assessing callers/background tasks.

Counterexample: authorize an MCP-only BC with an existing directory row; accept a status write; deliver its persisted future `ad_management.execute` payload repeatedly. Observe zero directory requests, REFRESH_PENDING/RUNNING and successive wakes. Pure execution of actual owner functions with synthetic boundaries reproduced three iterations: base ACCEPTED/0 wakes; HEAD REFRESH_PENDING/3 wakes; both zero reads. No DB reproduction was run. The baseline completed acceptance, so the waiting loop is new. P2 because this requires the missing-plan configuration.

## Scope and fixed evidence

Tree: `/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-m5a-native-status-validation`.
Branch: `feat/m5a-native-status-validation`.
Base and merge-base: `9d51cc9b37faf1b6a830fa025cbd377ace716048`.
HEAD: `d1bcbe60e46380ad5abf79b1792975120af4b62a`.

Exact comparison: `git diff 9d51cc9b37faf1b6a830fa025cbd377ace716048...d1bcbe60e46380ad5abf79b1792975120af4b62a`.
Verified refs, merge-base, eight commits and nine changed paths once against `review-r1-bundle.json`; no alternate origin/main baseline. The bundle source_candidate is an earlier phase candidate, not the reviewed HEAD; its explicit head matches.

Commits (newest first): d1bcbe60, 0ddc58c2, 24548ccf, f4c8fd50, c6694ca8, 2e3a52b6, 5f4b0166, 20709eb5.
Paths: formal-verify-management-status/SKILL.md; actions.py; execution.py; test_native_recovery.py; test_native_status.py; implementation-progress.md; both 2026-11-15 validation documents; frontend/tests/ads-detail.spec.ts (full paths as bundle).

Authoritative scope read: approved-scope.md, JB-59.md, JB-60.md; C01–C10 from `/tmp/tkada-native-series-20261112/m5a-ticket-map.json` and m5a-ticket-plan.md. Those two files are in the parent directory, not m5a-implementation. The older NOT_READY forced-preview proposal was not used as acceptance authority. Direct intake/asynchronous preparation and existing explicit restore preview remain distinct.

Standards read: AGENTS.md, CONTRIBUTING.md, docs/engineering-baseline.md, development.md, backend/README.md, frontend/README.md; applicable management standards in 2026-09-30-ads-management-reporting-design.md and 2026-10-30-bulk-management-design.md. No separate nested AGENTS/ADR files were found in this product tree. Authorized shared serial tree and the explicit Sol override are not deviations.

## Caller and state trace

Reviewed both product owners across the whole diff, not just the latest restore repair. Traced single and physical-batch outcome persistence, dispatch_work/task execution, task aggregation, claim/refresh wakeups, request attempts and receipt fencing, targeted directory scheduling/publication, public task projection, UNKNOWN reconciliation and restore preview/submission/current-value gates. Checked current tenant/BC/account/actor permissions and frozen route generation checks at write boundaries. Read automation-rule result aggregation: it consumes item execution results independently of the new observation state.

Batch success does not immediately call _set_refresh_state, but it already persists a future claim wake before sending. That wake subsequently routes accepted/pending observations through the single-item refresh path; absence of an immediate setter is not independently a lost-wakeup finding. UNKNOWN has priority over refresh_pending in final aggregation. Accepted refresh never reenters the status write path; UNKNOWN remains terminal for execution and its observations do not manufacture an ACCEPTED receipt/request ID. Restore intent uses the frozen original, current-original yields NO_CHANGE, current-sent-target stays eligible, and missing/unsafe values conflict. Original task values are not rewritten by these changes. These are source conclusions, not proof of every concurrent interleaving.

## Own checks and limits

Own: read-only Git/source/bundle review; exact-range `git diff --check` passed; Python 3.14 AST parsing of both product and both new Python test files passed. An initial system-Python 3.11 AST invocation rejected the repository’s Python 3.14 exception syntax; corrected to the original .venv interpreter. This was an interpreter mismatch, not a product failure.

Own pure counterexample: AST-extracted, unmodified actual _refresh_many_after_commit, _status_observed and _set_refresh_state functions from base and HEAD executed in memory. Session/model lookup, scheduling and summary boundaries were synthetic; no application import, DB, Redis, broker or provider call. The plan lookup returned None and the directory row predated the attempt. Three refresh/setter iterations produced the S1 output above. This proves the local branch behavior only; it is not integration or broker evidence.

Inherited, not rerun: JB59 root 12 HTTP checks; source-matched six UI cases/119 regressions and rejection probes. JB60 root 33 mixed/restore/revocation checks, plus matched five UI cases and remaining UNKNOWN/late-message/probe evidence. Read the two product validation records and test harnesses; their reported passes are not my execution results. SDK transport/authentication/time doubles remain synthetic. Actual HTTP/PG17/Redis/original Worker body/durable payload evidence does not prove independent broker delivery or real TikTok behavior. The bounded skill enumerates 20 restore inputs; no mathematical/unbounded TLC claim.

Preserved limits: START/candidate SQL-count26>25 failure remains inherited; early two directory timing failures retain Unknown cause. A green rerun does not establish stability or platform eligibility. Accepted-refresh route availability and multi-connection behavior have no independent service-level verification here; S1 is the evidenced missing-plan case, not a claim that all multi-connection cases fail.

No services, shared fixtures, DB/Redis tests, migrations, source mutations, staging, commits, tracker/parent writes, business network calls, real ads, push/PR/main merge or deployment. Original .venv and node_modules symlink targets were read and preserved. Only this external report was written; peer report untouched. One targeted Nowledge search returned no pertinent M5a decision; it supplied no review authority.
