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
