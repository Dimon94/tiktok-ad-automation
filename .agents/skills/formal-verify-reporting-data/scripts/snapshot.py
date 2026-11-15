"""R9 finite committed-observation exploration; SQL/permissions require PG replay."""

import argparse
from itertools import product

parser = argparse.ArgumentParser()
parser.add_argument("--broken-read", action="store_true")
parser.add_argument("--broken-export", action="store_true")
args = parser.parse_args()
count = 0
# Two query observations, three independently published layer versions, money/evidence axes.
for initial, latest in product(product(range(2), repeat=3), repeat=2):
    for evidence in range(2):
        frozen = (initial, 0)
        current = (latest, evidence)
        page = current if args.broken_read else frozen
        detail = current if args.broken_read else frozen
        # Export owns its captured tuple even after the query TTL removes its source.
        export = current if args.broken_export else frozen
        snapshot_exists = False
        assert page == detail == export == frozen, (initial, latest, evidence, snapshot_exists)
        count += 1
print(f"R9 {count} bounded snapshot observations passed")
