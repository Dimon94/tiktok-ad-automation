"""Finite explicit-state model and exhaustive generated inputs against real code."""
from collections import deque
from decimal import Decimal as D
from itertools import product
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend'))
from app.modules.business.calculation import derive_metrics


def amounts():
    count = 0
    # No Python Decimal SMT verifier is installed: run the real pure function.
    domain = [None, D(0), D('0.01'), D(50), D(100)]
    for spend, business, comp, subsidy, scenario, partial in product(domain, domain, domain, domain, ['base', 'comic', 'other', 'existing'], [False, True]):
        values = dict(spend=spend, revenue=business, business_revenue=business, compensation=comp, subsidy_comic=subsidy, subsidy_other=subsidy, subsidy_existing=subsidy)
        original = dict(values)
        result = derive_metrics(values, scenario=scenario, issues=['COMPENSATION_PARTIAL'] if partial else [])
        expected = business
        if scenario != 'base':
            expected = None if partial or any(v is None for v in [business, comp, subsidy]) else business + comp + subsidy
        assert result['scenario_revenue'] == expected, (values, scenario, partial)
        assert result['scenario_roas'] == (expected / spend if expected is not None and spend else None)
        assert result['compensation'] == comp and values == original
        count += 1
    return count


def snapshots(broken=False):
    # State: phase, contract, frozen rows, live rows, durable cursor, file rows.
    # A DB batch commit is atomic; file append before commit can crash separately.
    initial = [('QUEUED', None, (), (0, 1), 0, ())]
    initial += [('RUNNING', version, (0, 1), (0, 1), 0, file) for version in ['old', None] for file in [(), (9,)]]
    seen = set(initial)
    queue = deque(initial)
    while queue:
        phase, contract, frozen, live, cursor, file = state = queue.popleft()
        successors = []
        if phase == 'QUEUED':
            successors.append(('RUNNING', 'v1', live, live, 0, ()))
        elif phase == 'RUNNING':
            if contract != 'v1' and not broken:
                successors.append(('FAILED', contract, frozen, live, cursor, file))
            else:
                output = file[:cursor] + frozen[cursor:cursor+1]
                # Both crash before DB commit and successful DB commit.
                successors.append((phase, contract, frozen, live, cursor, output))
                advanced = cursor + 1
                successors.append(('COMPLETE' if advanced == 2 else phase, contract, frozen, live, advanced, output))
            successors.append((phase, contract, frozen, (2, 3), cursor, file))
        for nxt in successors:
            if contract != 'v1' and phase == 'RUNNING':
                assert nxt[5] == file, ('old snapshot appended', state, nxt)
            if nxt[0] == 'COMPLETE':
                assert nxt[5] == frozen and nxt[4] == 2, ('duplicate or live data', nxt)
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return len(seen)


if __name__ == '__main__':
    print(f'PASS real Decimal inputs: {amounts()}')
    print(f'PASS snapshot model states: {snapshots()}')
    try:
        snapshots(broken=True)
    except AssertionError:
        print('PASS model negative control: incompatible append rejected')
    else:
        raise AssertionError('negative control missed')
