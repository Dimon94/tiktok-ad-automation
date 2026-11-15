"""JB36 finite accounting model. Assumes atomic RR capture, publisher evidence,
exact identity and full independent parent selection. Not a platform proof.
"""
import argparse
from decimal import Decimal
from itertools import product

p = argparse.ArgumentParser()
for name in ('scope', 'cross-scope', 'nullzero', 'parent-total'):
    p.add_argument('--broken-' + name, action='store_true')
a = p.parse_args()
count = 0
for selected_full, same_currency, same_window, observed, second_known in product((False, True), repeat=5):
    spend = (Decimal(2), Decimal(8))
    revenues = (Decimal(1), Decimal(8) if second_known else None)
    known = [r for r in revenues if r is not None]
    numerator = sum(known, Decimal(0)) if known else None
    ratio = numerator / sum(spend) if numerator is not None else None
    assert ratio == (Decimal('.9') if second_known else Decimal('.1'))
    comparable = (selected_full or a.broken_scope) and (same_currency or a.broken_cross_scope) and same_window and observed
    difference = Decimal(0) if comparable else None
    if not (selected_full and same_currency and same_window and observed):
        assert difference is None, 'cross-scope numerical difference'
    unknown = Decimal(0) if a.broken_nullzero else None
    assert unknown is None, 'unknown became zero'
    ordinary_total = sum(spend) + (Decimal(10) if a.broken_parent_total else Decimal(0))
    assert ordinary_total == 10, 'control entered material total'
    count += 1
print(f'JB36 {count} bounded metric/reconciliation cases passed')
