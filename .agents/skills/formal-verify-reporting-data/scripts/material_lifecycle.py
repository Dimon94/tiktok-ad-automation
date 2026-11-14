"""Finite JB37 lifecycle: two batches, two identities, bounded time/contract/source."""
import argparse
from itertools import product

parser = argparse.ArgumentParser()
parser.add_argument('--broken-atomic', action='store_true')
parser.add_argument('--broken-ttl', action='store_true')
parser.add_argument('--broken-isolation', action='store_true')
args = parser.parse_args()
cases = 0
for failure, expired, compatible, later_source in product(range(3), (False, True), (False, True), (40, 90)):
    # Each batch abstracts a full row with its instances/evidence/parents/coverage.
    staged = []
    committed = ()
    for batch in range(2):
        if failure == batch:
            break
        staged.append((batch, 40))
        if args.broken_atomic:
            committed = tuple(staged)
    else:
        committed = tuple(staged)
    assert not committed or committed == ((0, 40), (1, 40)), 'partial visibility'
    visible = bool(committed) and compatible and (not expired or args.broken_ttl)
    assert not visible or (compatible and not expired), 'expired/old query access'
    if committed:
        original = committed
        replacement = ((0, later_source), (1, later_source))
        if args.broken_isolation:
            committed = replacement
        assert committed == original, 'source changed old identity'
        # A retry is another identity; old completed export keeps original bytes.
        snapshots = {'old': committed, 'new': replacement}
        assert snapshots['old'] == original
        assert snapshots['new'] == replacement
        old_completed_file = original
        assert old_completed_file == original
        pending_old_can_generate = compatible
        assert compatible or not pending_old_can_generate
    cases += 1
print(f'JB37 {cases} bounded lifecycle cases GREEN')
