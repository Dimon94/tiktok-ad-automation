"""R1: account coverage and precise fact observations are different coordinates.

Atomic transition maps to publish_ad_day's existing PG transaction/advisory lock;
query capture maps to the existing RR INSERT/format/freeze transaction. The
model assumes that atomicity; native_publication tests bind it to real PG.
"""

import argparse
from itertools import product

parser = argparse.ArgumentParser()
parser.add_argument("--broken-account-stamp", action="store_true")
args = parser.parse_args()
count = 0
for full, payload, contract in product((False, True), range(2), ("native", "legacy")):
    coverage = ("t0", "V", "H") if full else None
    # A fact is a single immutable abstract (money token, observation) pair.
    first = ("old-a1", ("t0", "account", "H", contract)) if full else None
    other = ("old-a2", ("t0", "account", "H", contract)) if full else None
    before = (coverage, first, other)
    precise = ("t1", "C1", f"h{payload}", "native")
    selected = ("new-a1", coverage if args.broken_account_stamp else precise)
    after = (coverage, selected, other)
    assert after[0] == before[0] and after[2] == before[2]
    assert after[1][1] == precise, (before, after)
    derived = tuple(row[1] for row in (selected, other) if row)
    assert derived[0] == precise
    if full:
        assert derived[1] != precise  # multiple observations, never one account refresh
    # Frozen detail/export do not follow the next fact replacement.
    frozen = (after, derived)
    current = (coverage, ("later-a1", ("t2", "C1", "later", "native")), other)
    detail = export = frozen
    assert detail == export == frozen and current != after
    count += 1
print(f"R1 {count} bounded precise-scope observations passed")
