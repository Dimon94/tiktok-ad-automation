---
name: formal-verify-business-export
description: Verify business export Decimal NULL/zero semantics and frozen snapshot resume compatibility when calculation, queries, snapshot or writer changes.
---

Run from TK-ADA root, using the locked Python 3.14 environment:

```sh
uv run --frozen python .agents/skills/formal-verify-business-export/verify.py
bash scripts/test.sh tests/modules/business/test_export_integrity.py tests/modules/business/test_exports.py -q
```

Set DATABASE_URL to a dedicated local test PostgreSQL database and TEST_REDIS_URL
to an isolated nonzero Redis database, following deploy/ci/check.sh. Never load
production settings. A task environment may use UV_PROJECT_ENVIRONMENT.

Properties: unknown compensation never becomes a complete scenario amount; zero
remains zero. Frozen rows survive source changes and replay without duplicates;
incompatible snapshots cannot append bytes. Otherwise exported reports can cause
incorrect spending decisions.

Coverage mapping:
- calculation.py derive_metrics: 5,000 generated real-code inputs, all combinations
  of NULL/0/0.01/50/100, four scenarios and partial/complete issues; input unchanged.
- export_snapshot.py freeze_rows: model QUEUED -> RUNNING atomic DB freeze.
- export_writer.py write_batch: model file append can precede DB cursor commit;
  next attempt truncates to the persisted cursor. Two frozen row identifiers.
- exports.py process_pending_export: model incompatible RUNNING -> FAILED;
  terminal states have no outgoing writes. Integration tests bind model to real code.

The stdlib explicit-state checker explores 27 reachable states, including changes
to live rows and crashes before commit. Chosen for the small finite state space;
no external prover dependency. Decimal Python has no installed code verifier, so
amounts use exhaustive bounded inputs on the actual function, not a copied model.
This is bounded verification, not a proof for all Decimal values or the system.
Assumptions: one locked writer, atomic DB savepoint, durable ordered file writes,
two rows, no disk loss/S3/network/permission races in the model. Existing real
PG/Redis and export tests cover additional behavior. No liveness proof.

Negative checks: the model runs with its compatibility guard removed and must
produce an old-snapshot-append counterexample. For real-code sensitivity, back up
calculation.py outside the repo, remove COMPENSATION_PARTIAL masking, run verify.py
(expect failure), restore byte-for-byte. Back up export_writer.py, bypass its
contract guard, run test_incompatible_snapshot_fails_before_append (expect failure),
restore and rerun green. Never commit mutations. A model/code disagreement is a
finding: trace freeze -> append -> crash -> resume and inspect actual transaction
boundaries before changing assumptions.

Sync record: reviewed against baseline 9ebdeae0f67563e4e8f8152f4459559bdc59bd17
plus the JB-3 implementation in the commit introducing this skill (2026-11-12).
Move this record to the next commit only after reviewing the mapping above.
Use maintain-formal-verification-skill when the mapped functions change.
