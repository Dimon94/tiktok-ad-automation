"""R7 finite bucket product, two workers and observations; stdlib explicit exploration."""
from collections import deque
import sys

broken = sys.argv[1] if len(sys.argv) > 1 else ''
total = 0
# Buckets factor: no cross-bucket write exists; check each of three level components.
for level in ('campaign', 'adgroup', 'ad'):
    # fence, watermark, publication version, fact, evidence version, attempts bitmask
    initial = (0, 0, 0, None, 0, 0)
    queue, seen = deque([initial]), {initial}
    while queue:
        state = queue.popleft()
        fence, watermark, version, fact, evidence, attempts = state
        assert version == evidence, ('atomic evidence', level, state)
        for worker in (1, 2):
            successors = [(worker, watermark, version, fact, evidence, attempts)] if worker > fence else []
            for observation in (1, 2):
                for payload in ('VALUE', 'EMPTY'):
                    # Bit distinguishes same-time conflicting payloads and exact replays.
                    bit = 1 << ((observation - 1) * 2 + (payload == 'EMPTY'))
                    if fence != worker and broken != '--broken-fence':
                        continue
                    if observation < watermark and broken != '--broken-watermark':
                        continue
                    if observation == watermark:
                        if fact != payload:  # explicit conflict, no write
                            continue
                        continue  # exact replay, no write/version increment
                    if attempts & bit:
                        continue
                    # Duplicate platform identities must fail before publication.
                    for duplicate in (False, True):
                        if duplicate and broken != '--broken-duplicate':
                            continue
                        successor = (fence, observation, version + 1, payload,
                                     version if broken == '--broken-atomic' else version + 1,
                                     attempts | bit)
                        assert fence == worker, ('lost fence published', level, state, successor)
                        assert observation >= watermark, ('late overwrote', level, state, successor)
                        assert not duplicate, ('duplicate published', level, state, successor)
                        successors.append(successor)
            for successor in successors:
                assert successor[2] == successor[4], ('atomic evidence', level, state, successor)
                if successor not in seen:
                    seen.add(successor)
                    queue.append(successor)
    total += len(seen)
print(f'R7 two-worker/two-observation/three-level factor model: {total} component states; PASS')
