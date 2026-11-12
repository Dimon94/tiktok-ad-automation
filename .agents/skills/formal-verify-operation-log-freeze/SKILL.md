---
name: formal-verify-operation-log-freeze
description: Verify M2 account-local necessary log selection, immutable frozen sources, semantic signatures and publication isolation when operation log freezing changes.
---

Run from the TK-ADA repository root:

```sh
python3 .agents/skills/formal-verify-operation-log-freeze/verify.py
```

Python stdlib exhaustive enumeration is used because this is a finite source-selection
and freeze/correction model, not a proof of unbounded concurrency. Two accounts,
three local dates (-1, 0, 1), one requested day, selected anchor on each date,
and absent/two semantic content versions give 78732 source transitions.
Frozen tuples represent immutable export identities plus semantic content; identical
content replay is the same semantic state. Each transition corrects one source,
then checks old restoration, new signature, and failed/superseded/verified publication.
All five broken model variants must fail. The harm guarded is missing a justified
estimate or rewriting a previously published result with unfrozen evidence.

Sync record, confirmed 2026-11-12: baseline commit
`af24c3b968b9fa5cdbd8c55c9e5df4f00478df6f` plus the sole changed product file
`backend/app/modules/operation_logs/evidence.py`, Git blob
`6d081745147fa008c3d5ed0fa65e57f6c8b1174c`. This exact source mapping describes
the repaired implementation, not the unfixed baseline. Other mapped owners are
unchanged from that commit. Verify with `git hash-object` when reusing this record.
M2 dynamic evidence is in docs/validation/2026-11-12-jb6-historical-log-evidence.md.

Coverage mapping:
- evidence.py `frozen_sources`: necessary dates per account, content signature.
- evidence.py `build_evidence`: reads only frozen export/observation identities.
- business/projection.py and projection lifecycle: candidate verification and publication.

Atomic assumptions: database transactions, immutable stored exports/observations,
fixed selected anchors, authoritative tenant/account filtering. Model excludes anchor
selection, IANA conversion (real PG cases cover DST), money/formulas, authorization,
Redis/worker races, large ranges and file storage. It does not prove the whole system.

Bind the model to real code using the dedicated local PG/Redis environment:

```sh
source /tmp/jb6-env.sh
cd backend
python -m pytest tests/modules/operation_logs/test_historical_evidence.py tests/modules/business/test_historical_log_rebuild.py -q --tb=short
```

The `/tmp` environment is local-only; recreate the isolated test environment from
the validation record if it no longer exists. Never load a production environment.
Then copy evidence.py outside the tree, replace the per-account
left boundary with start and run the primary case: it must fail with NULL, not 50.
Restore with `cp`, verify `cmp`, then rerun green. The validation record also specifies
the early-signature mutation and its real-code current/source checks. Never accept
model success as a replacement for this seam. If an invariant fails, report its
counterexample; adjust the model only when the approved contract changes. Confirm
this mapping again on owner changes before moving the sync commit forward; use
maintain-formal-verification-skill for later maintenance.
