"""Bounded R4/R10 model; does not execute or prove production SQL."""
from collections import deque
import sys

broken = "--broken-selector" in sys.argv
initial = (None, None, None)
queue = deque([initial])
seen = {initial}
while queue:
    state = queue.popleft()
    campaign, adgroup, child = state
    for level, direct in enumerate((campaign, adgroup)):
        selected = child if direct is None or (broken and direct == "DASH") else direct
        if direct is not None:
            assert selected == direct, ("direct authority", state, level, selected)
    for index, values in ((0, ("40", "DASH", "EMPTY")), (1, ("40", "DASH", "EMPTY")), (2, (None, "999", "DASH"))):
        for value in values:
            successor = list(state)
            successor[index] = value
            successor = tuple(successor)
            if index == 2:
                assert successor[:2] == state[:2], "derived writer changed parent"
            if successor not in seen:
                seen.add(successor)
                queue.append(successor)
print(f"R4/R10 finite model: {len(seen)} states; PASS")
