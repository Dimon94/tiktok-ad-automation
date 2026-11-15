# M4b Standards independent review — R1 / max 3

**Verdict: PASS. Findings: P0=0, P1=0, P2=1, P3=0.**

Only Standards was reviewed. This is an independent source review, not implementer self-assessment, Spec acceptance, whole-M4b release approval or permission to integrate/deploy. Five ticket-stage acceptances remain inherited evidence. Reviewer performed the work directly using Codex gpt-6.1-sol/high; no child agents.

## Fixed scope and integrity

- Source tree: `/Users/dimon/02-Work/10-Projects/ads-decision-system/projects/tiktok-ad-automation.wt/feat-jb33-native-material-m4b`
- Branch: `feat/jb33-native-material-m4b`
- Baseline: `1c987867971e8340ff793597b3c42c6acbb6fecc`
- Reviewed head: `8d3629b9864389d0e8a6ff4b1b8ab53bd43d5c3e`
- Exact command: `git diff 1c987867971e8340ff793597b3c42c6acbb6fecc...8d3629b9864389d0e8a6ff4b1b8ab53bd43d5c3e`
- Personally verified both refs, actual HEAD, branch, merge-base=baseline, all 50 changed paths and all 16 commits against `/tmp/tkada-native-series-20261112/m4b-implementation/review-r1-bundle.json`. Paths and commit-list comparisons both returned True.
- Beginning and ending source status: only the declared untracked `.venv` and `node_modules` links. No source edits, staging, commits, migrations, servers, shared integration fixtures, mutation probes or external/platform writes.

## Finding and runnable counterexample (P2/P3 combined below 200 words)

**S1 · P2 — Removed creative makes stale-projection queries fail.**

`backend/app/modules/reporting/material_query.py:521–524`, new join `creative_query.py:167–170`: after `publish_creative_day` removes C (`publish.py:648–660`), its old projection survives until asynchronous rebuild. LEFT JOIN yields `fact_observed_at=None`; eager `.get` default evaluation raises despite existing projected publication evidence. `_build_snapshot → freeze_material_results → freeze_instance_tracer` fails; page-cache returns query failure.

Violates `AGENTS.md:60` (assess caller/data/background-task effects), breaking existing stale-result handling (`read_models/coverage.py:105`, `queries.py:287–317`). Source triggers (`alembic/versions/0113_creative_material_projection.py:107`) only dirty buckets; they do not synchronously rebuild projections.

Personally replayed the exact AST expression: AttributeError. Minimal equivalent:

```python
f = {'evidence': {'publication': {'observed_at': '2026-11-14T00:00:00+00:00'}},
     'fact_observed_at': None}
((f.get('evidence') or {}).get('publication') or {}).get(
    'observed_at', f['fact_observed_at'].isoformat())
```

Baseline has neither join nor tracer: new issue. Checked other changed timestamp reads; no identical occurrence. Preserve projected observation coordinates without requiring surviving current source rows. P2: specific refresh window. Full PG transition was traced, not dynamically executed.

## Standards and main-path coverage

Read the six requested standards sources: `AGENTS.md`, `CONTRIBUTING.md`, `docs/engineering-baseline.md`, `development.md`, `backend/README.md`, `frontend/README.md`; followed the applicable local bootstrap/deployment guidance. Repository instructions take priority over upstream template examples. The authorized shared serial tree and requested Sol override are not deviations. No Spec findings or taste/formatting findings are included.

Traced the production diff and its existing callers across:

- Creative publication: official SDK job route passed into evidence; duplicate economic-fact rejection; per-day replacement/publication state; nullable evidence migration and projection preservation. No historical evidence backfill is claimed.
- Identity and metrics: tenant/advertiser fact keys; BC/current-grant checks and connection ambiguity; unresolved/composite identities kept separate; direct creative vectors; independent full-parent controls; reconciliation scope, observation and field-coverage checks; unknown Native Growth contracts; Decimal and denominator handling via the existing metric owner.
- Query lifecycle: RR construction, frozen rows, summary and publication map; shared contract checks for explicit reads and reuse; actor/tenant/BC scope and current grants; TTL; page-cache lease-bound failure signals; frontend explicit requery and hiding failed data.
- Export lifecycle: actor/tenant/BC and account checks at creation/generation/download; snapshot result copy; deterministic task-scoped object key; row locks and duplicate COMPLETE short circuit; failed original outbox rearm; object-write exception cleanup; write-after authorization/expiry check; old complete objects and pending contract rejection; CSV instance/special/control/difference separation.
- Schema migration and generated frontend types match the new nullable evidence and frozen result fields. Existing owners remain canonical; no new platform channel or parallel persistence/export state machine is introduced.

These paths yielded no evidenced P0/P1. No baseline downgrade was needed for a P0/P1 candidate; S1 was independently checked against the fixed baseline and is new.

## Personally executed vs inherited evidence

Personally executed read-only Git checks, source tracing, the AST replay above, and `git diff --check` (passed). Personally ran these standalone bounded models from the fixed tree:

| Command suffix | Result |
| --- | --- |
| `.agents/skills/formal-verify-reporting-data/scripts/material.py` | JB34 32 and JB35 12 bounded cases passed |
| `.agents/skills/formal-verify-reporting-data/scripts/material_metrics.py` | JB36 32 bounded cases passed |
| `.agents/skills/formal-verify-reporting-data/scripts/material_lifecycle.py` | JB37 24 bounded cases passed |
| `.agents/skills/formal-verify-reporting-data/scripts/material_export.py` | 64 bounded cases passed |

These models assume finite identities/times, correctly captured committed facts and bounded storage/transaction behavior. They do not prove PostgreSQL/Redis implementations, arbitrary concurrency, platform semantics or production capacity. No source-mutation scripts were executed.

Inherited, not rerun: the five validation records listed in the bundle (JB34 tracer, JB35 identity, JB36 metrics, JB37 query lifecycle, JB38 export lifecycle), including their root-stage acceptance sections. Their reported root checks are 22+3 / 21+6 / 13 / 12 / 21 respectively; their browser, HTTP, real local PostgreSQL/Redis, original Workers and local object fallback outcomes are documentation evidence, not personal executions in this review. Prior source-bound rejection probes and builds likewise remain inherited. This reviewer did not treat passing tests or phase acceptance as the independent review verdict.

## Verification limits and handoff

Shared PG/Redis fixture suites, browser/server runs, migrations and source-mutation probes were intentionally not run concurrently with the peer review. The pure S1 replay verifies the precise Python failure; its full database transition remains dynamically untested by this reviewer.

Synthetic local authentication and SDK transport doubles do not establish real authorization/provider integration. Remote S3 failure behavior, creativeNativeGrowth support, D0/window, traffic composition, total maturity and production capacity remain accepted Unknown/UNCONFIRMED, not product findings. No real platform or production call occurred.

Standards R1 is complete with PASS under the stated gate (no unresolved P0/P1). S1 is a nonblocking P2 for the caller to track; this report does not resolve/close any issue, alter Spec, assert Spec-axis acceptance or declare whole M4b released.

## Exact reviewed commits (bundle order)

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
