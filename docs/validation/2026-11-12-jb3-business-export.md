# JB-3 implementation evidence — 2026-11-12

Scope: JB-2/JB-3 latest full Linear bodies read; both comment lists empty.
Existing authorized tree: `projects/tiktok-ad-automation.wt/fix-jb2-business-integrity`,
branch `fix/jb2-business-integrity`, starting HEAD `9ebdeae0f67563e4e8f8152f4459559bdc59bd17`.
Only original untracked `.venv` and `node_modules` existed; both preserved.

## Implementation

Calculation `derive_metrics` owns the unchanged Decimal formulas and the non-base
COMPENSATION_PARTIAL mask. Queries and Writer call it with full-filter issues;
compensation subtotal remains available, NULL remains unknown, zero remains known.
No new public schema, bool, CSV total row, database migration or M2 work.
Snapshot JSON stamps `scenario-completeness-v1` only when freezing, strips its
internal key before filter validation, and does not refreeze frozen rows. Writer
rejects a missing/different contract before directory/file creation, truncation or
append; the existing orchestrator marks FAILED. Old unfrozen jobs freeze normally.

DRY record:
- scope: metric ownership, compatibility guard, formal verification.
- searched: business Calculation/Queries/Export Snapshot/Writer/Exports callers;
  repository scripts, tests, docs and .agents skills (no matching formal skill).
- reused: existing formulas (moved, not copied), issues, JSON metadata, HTTPException
  failure path, durable cursor/offset recovery, test fixtures and scripts/test.sh.
- remaining duplication: independent test/model expectations intentionally express
  JB-2's contract; no parallel production calculation owner.

## Real-code validation

Python 3.14.2 task environment `/tmp/jb3-venv`, created with
`UV_PROJECT_ENVIRONMENT=/tmp/jb3-venv uv sync --frozen --package app --python 3.14 --offline`.
No dependency/lock changes. Owned Docker `jb3-pg` (postgres:18-alpine), `jb3-redis`
(redis:7-alpine), random loopback ports; DB `jb3_test`, separate Redis DB 1.
APP_ENV_FILE=/dev/null, cloud credentials disabled. No real provider/production calls.
Environment setup `/tmp/jb3-env.sh` is local-only, not a shipped credential artifact.

Commands from the project root (after loading isolated environment):

The project-root `scripts/test.sh` forwards arguments to pytest. The distinct
template `backend/scripts/test.sh` always selects `tests/`; root initially ran
that wrong entrypoint from the backend directory and stopped that broad run.
This setup mistake is not a test result and does not invalidate the implementer
commands below. Root independently ran direct targeted pytest as recorded in
the addendum.

```sh
bash scripts/test.sh tests/modules/business/test_export_integrity.py tests/modules/business/test_queries.py tests/modules/business/test_exports.py tests/modules/business/test_api.py tests/modules/business/test_calculation.py tests/modules/business/test_concurrency.py -q --tb=short
# 93 passed in 102.68s, before the two final downgrade cases were added.
bash scripts/test.sh tests/modules/business/test_export_integrity.py -k 'downgrade or (spend0 and compensations0 and comic)' -q --tb=short
# 4 passed (two new downgrade cases and two representative matrix cases).
bash scripts/test.sh tests/modules/business/test_export_integrity.py -k 'incompatible or frozen_values' -q --tb=short
# 5 passed after restoring contract mutation.
```

Main regression: 64 combinations of campaign/day, four scenarios, 50/NULL,
NULL/NULL, 50/0, 0/0, spend 100/0. Real database facts -> query -> HTTP create ->
real batched writer -> HTTP streaming download -> csv.DictReader, compared with
paged queries. Includes full totals under changed sort; known subtotal and issue
text asserted. No CSV totals row introduced. Two day-level inputs aggregate to the
specified C=200/V=160/known compensation=50 counterexample.

Frozen-values test changes source amounts/issues after first batch, injects a
crash tail, resumes, checks exact order/count/values and byte-identical completed
file on repeated processing. Existing export test also loses its partial file
and reconstructs from frozen rows, verifies rounding total 800. Four old snapshot
cases cover missing/mismatched version and no/partial bytes, FAILED/no append/no
download/no automatic retry. Existing API/permission tests and new pending export
downgrade cases preserve admin/tenant/account boundaries; real concurrency suite
uses PostgreSQL and Redis. Freeze/write infrastructure exception injection was not
newly added; those handlers were not modified.

Red and mutation evidence (local logs under /tmp):
- `jb3-red-metrics.log`: genuine CSV 130.0000 versus blank assertion failure.
- `jb3-red.log`: four legacy snapshots continued RUNNING instead of FAILED.
  Earlier fixture date and API path mistakes were corrected; not counted as red.
- `jb3-mutation-amount.log`: removing real Calculation mask fails generated input.
- `jb3-mutation-csv.log`: removed shared mask yields totals 210 instead of NULL.
- `jb3-mutation-csv-only.log`: Writer drops frozen issues; actual CSV comparison
  fails, including day aggregate 210.0000 instead of blank.
- `jb3-mutation-contract.log`: bypass Writer guard; all four incompatible cases fail.
- Each production mutation restored from outside-tree backup with successful cmp;
  `jb3-restored.log`, `jb3-final-targeted.log`, `jb3-final-green.log` record green.

Ruff on four changed production files and new test: PASS. git diff --check: PASS.
`ty` is not green: existing calculation subsidy tuple return type mismatch reproduced
from starting HEAD (`/tmp/jb3-baseline-ty.log`), plus existing SQLModel execute
warnings. No claim of full-repository static/test success.

## Bounded verification

Generated and followed `.agents/skills/formal-verify-business-export/SKILL.md`.
`/tmp/jb3-venv/bin/python .agents/skills/formal-verify-business-export/verify.py`:
5,000 actual Decimal function inputs PASS; 27 finite snapshot states PASS;
model negative control catches incompatible append. Real-code mutations above
bind checks to the implementation. Python stdlib enumeration avoids a new prover
dependency. This is bounded verification, not a whole-system proof. Assumes one
locked writer, atomic DB commits, durable ordered local file writes, two rows;
excludes S3, disk loss, permissions and arbitrary Decimal values from the model.
The real integration tests remain primary acceptance evidence.

## Browser and remaining acceptance

T3 preview_status -> preview_open -> snapshot/act used exclusively. Created
isolated `jb3_browser_test`, reused fixture seeding, real API on 18033 and Vite on
18034. Login succeeded. BC picker listed local synthetic bc-report-case, but selecting
it (and direct business URL with bc_id) returned to the no-BC prompt; therefore no
actual webpage query/export/download completed. Browser acceptance is **Unknown**.
Do not treat the HTTP CSV regression or existing mocked Playwright suite as browser
acceptance. Next: resolve the isolated fixture/router BC selection, then compare
real webpage blanks/subtotal with its downloaded CSV. No unrelated frontend fix made.

Setup failures: Docker initially stopped; started Desktop. Credential helper absent
from PATH during image pull; used existing cached PG18/Redis7 images. Initial browser
migration command had relative script_location; reran with explicit script location.
Vite reported a font 403 through the existing node_modules symlink. These are recorded
as setup issues, not application regression results. Temporary resources only; no
other container changed. API/Vite and owned test containers stopped after evidence.

Not performed: production, TikTok, push, PR, deployment, Linear writes, independent
root integration/Standards/Spec review. Browser gap prevents claiming full JB-3
acceptance. Root owns final review and Linear disposition.

## Root browser acceptance addendum

The earlier BC-selection gap was a test-data mismatch: the frontend tenant route
accepts numeric BC IDs, while the reused reporting fixture used `bc-report-case`.
No frontend or product fix was needed. Root reused the isolated browser database
and seeded a numeric synthetic BC, retaining the same authenticated test user.

Using T3 preview tools only, root opened the actual local Vite/API page, selected
2026-11-02 and the comic scenario, and observed spend 200.00, platform revenue
160.00, compensation subtotal 50.00, and blank scenario income/ROAS. Root clicked
the real Export CSV button, executed the normal pending-export worker against
that isolated database, and let the browser finish its normal polling/download.
The actual downloaded `business.csv` (465 bytes) was parsed with `csv.DictReader`:
one row, compensation `50.00`, both scenario cells empty, and
`COMPENSATION_PARTIAL` retained. This closes the representative real-browser
query/download gap; it is synthetic acceptance data, not live account data.

The owned browser tab was closed and the root-owned local API/Vite processes
were stopped. The bounded verification command was independently rerun: 5,000
real Decimal inputs, 27 model states, and the incompatible-append negative
control passed. Independent Standards/Spec review remains pending at this point.

## Root independent regression

From `backend/`, after sourcing the isolated environment, setting
`APP_ENV_FILE=/dev/null` and prepending `/tmp/jb3-venv/bin` to PATH:

```sh
/tmp/jb3-venv/bin/python -m pytest tests/modules/business/test_export_integrity.py tests/modules/business/test_queries.py tests/modules/business/test_exports.py tests/modules/business/test_api.py tests/modules/business/test_calculation.py tests/modules/business/test_concurrency.py -q --tb=short
```

Result: **95 passed in 122.02s**. This includes the two final downgrade tests.
Log: `/tmp/jb3-root-suite.log`. An initial attempt with the system-Python PATH
failed importing Alembic; it was an environment setup failure, not a product
failure or a passing run.

Standards R1 on fixed source commit `ce025a76` passed with zero P0/P1/P2/P3.
The reviewer independently checked 48 pure-function cases and did not rerun
the database suite. Spec review is still pending at this recording point.
