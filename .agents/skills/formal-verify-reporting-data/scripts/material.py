"""JB34 finite material tracer: independent parent, full scoped identity, frozen read.

Assumes atomic RR capture and existing authorization. Does not prove SQL or
platform semantics; material_instance_tracer PG and SDK/browser replay bind it.
"""
import argparse
from itertools import product

parser = argparse.ArgumentParser()
parser.add_argument("--broken-parent", action="store_true")
parser.add_argument("--broken-scope", action="store_true")
parser.add_argument("--broken-freeze", action="store_true")
parser.add_argument("--broken-double-count", action="store_true")
parser.add_argument("--broken-null-to-zero", action="store_true")
args = parser.parse_args()
count = 0
for tenant, bc, connection, amount, newer in product(range(2), repeat=5):
    identity = (tenant, bc, connection, "account", "ad", "vid", "mid")
    # BC/connection select a fact; they do not create a new additive money token.
    fact_key = (tenant, "account", "creative", "localday")
    creative = (40 + amount, fact_key)
    parent = 100
    captured_identity = (0, *identity[1:]) if args.broken_scope else identity
    captured = (captured_identity, parent if args.broken_parent else creative[0], fact_key)
    frozen = captured
    current = (identity, 90 + newer, fact_key)
    page = current if args.broken_freeze else frozen
    detail = csv = frozen
    assert captured[0] == identity, "scope changed"
    assert captured[1] == creative[0] != parent, "parent borrowed"
    assert page == detail == csv == captured, "frozen result changed"
    count += 1
print(f"JB34 {count} bounded material tracer cases passed")

# JB35: two instances, five unique creative facts, duplicate references and
# special identities. Tokens stand for immutable committed PG facts; selection
# and atomicity remain assumptions bound by the actual query/CSV regressions.
from collections import Counter
from decimal import Decimal


# Existing CLI flags continue to drive the original bounded tracer above.
for duplicates, special, legacy in product((1, 2, 3), (False, True), (False, True)):
    facts = [("c0", Decimal(10), "VALUE"), ("c1", None, "NULL"), ("c2", None, "DASH"), ("c3", None, "ABSENT"), ("c4", Decimal(7), "LEGACY_UNKNOWN" if legacy else "VALUE")]
    references = facts * duplicates
    unique = {str(index) if args.broken_double_count else f[0]: f for index, f in enumerate(references)}
    if args.broken_null_to_zero:
        unique = {k: (f[0], Decimal(0) if f[1] is None else f[1], "VALUE" if f[1] is None else f[2]) for k, f in unique.items()}
    money = sum((f[1] for f in unique.values() if f[1] is not None), Decimal(0))
    counts = Counter(f[2] for f in unique.values())
    assert money == 17 and sum(counts.values()) == 5, "duplicate fact counted"
    assert counts["NULL"] == counts["DASH"] == counts["ABSENT"] == 1
    assert counts["LEGACY_UNKNOWN"] == int(legacy)
    # A's five facts and B's separate creative remain distinct even when their
    # references repeat. A special combination never assigns its money to B.
    instance_b = {"c5": Decimal(7)}
    buckets = {"A": money, "B": sum(instance_b.values())}
    ordinary_total = buckets["B"] + (Decimal(0) if special else buckets["A"])
    assert ordinary_total == (7 if special else 24), "special bucket became instance"
    assert set(unique).isdisjoint(instance_b), "creative counted in two instances"
print("JB35 12 bounded duplicate/five-state/special cases passed")
