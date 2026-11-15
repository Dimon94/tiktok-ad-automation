---
name: formal-verify-management-status
description: Recheck bounded Native management restore and UNKNOWN invariants when changing prepare_restore or reconciliation ownership.
---

# Bounded management status verification

This is finite input enumeration against the real Python production owner with PostgreSQL and Redis, not a mathematical proof or a hand-written model. Python has no verifier configured here; use the existing HTTP seam rather than claim Build TLC covers management.

Property: restore uses the source task's frozen original status and sends only when current status equals its frozen sent target. Already at the original means no change; missing or unsafe current means conflict. UNKNOWN is execution evidence and cannot become acceptance through observation or recovery.

Owner: `backend/app/modules/ad_management/actions.py:prepare_restore`, the existing submissions owner and `execution.py:_same_frozen_value`. `test_native_restore_current_gate` in `backend/tests/modules/ad_management/test_native_recovery.py` enumerates both frozen originals ENABLE/DISABLE, both source outcomes ACCEPTED/UNKNOWN and five current observations (sent target, original, absent, None, changed to DELETE): 20 cases. It uses real SDK-boundary outcomes and actual directory publication before changing current observations to model outside interference. Every case submits the existing restore preview, asserts physical writes and checks source immutability. No new async state machine is modeled.

Assumptions: one original write and one explicit restore; finite status alphabet; synthetic authentication/SDK and current interference; no crash inside a SQL commit. No unbounded liveness, real provider or all multi-actor interleavings proved. Original UNKNOWN is not replayed; reverse intent is a separate preview/task.

Run from backend with the root-owned dedicated environment:

```sh
python3 /tmp/tkada-native-series-20261112/m5a-implementation/run-test.py python -m pytest tests/modules/ad_management/test_native_recovery.py -q -k 'restore_current_gate or unknown_rounds'
```

Source sync commit: `c6694ca837650c77efc0f21db2d8b51dddd10ccc`, based on `2e3a52b6fb278fb924b3f21cbc0fe31e01243662`. Source owner and 20 cases confirmed in `jb60-validation.log` (64 passed); actual source conflict mutation rejected four unsafe cases and targeted green passed after finally/cmp. Before advancing the sync commit confirm the real owner still has the current/frozen comparison and the HTTP test still submits actual previews. Restore mismatch source probe removes the actual `status is None` conflict branch: unsafe preview must fail its business assertion. Other source probes cover UNKNOWN replay, false acceptance, failed sibling blocking and reads issuing writes. External backup/finally restore/cmp plus targeted green is mandatory; syntax/import errors are not counterexamples. Logs and runner: `/tmp/tkada-native-series-20261112/m5a-implementation/jb60-probes*` (local evidence, not portable or credentials).
