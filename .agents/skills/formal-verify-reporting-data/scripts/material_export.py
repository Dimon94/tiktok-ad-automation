"""Finite export publication model; committed payload and atomic object put assumed."""
import itertools
import sys

broken = set(sys.argv[1:])
count = 0
for frozen, live, before, after, expired, scope in itertools.product(
    (40, 90), (40, 90), (False, True), (False, True), (False, True), (False, True)
):
    content = live if '--broken-live' in broken else frozen
    object_exists = not before
    complete = object_exists and not after and (not expired or '--broken-expiry' in broken)
    if after and '--broken-partial' not in broken:
        object_exists = False
    readable = complete and (scope or '--broken-scope' in broken)
    assert not readable or content == frozen, 'live reread'
    assert not after or not object_exists, 'failed publication left object'
    assert not expired or not complete, 'expired publication'
    assert scope or not readable, 'cross scope download'
    count += 1
print(f'{count} bounded export cases passed')
