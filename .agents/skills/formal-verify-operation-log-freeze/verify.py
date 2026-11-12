"""Bounded state exploration for JB-4 R1/R2/R3/R5; no third-party prover."""

from itertools import product

DAYS = (-1, 0, 1)


def freeze(store, anchors, mutation=None):
    frozen = []
    for account, anchor in enumerate(anchors):
        left = 0 if mutation == "boundary" else min(0, anchor)
        right = max(0, anchor)
        if mutation == "scope":
            left, right = min(0, *anchors), max(0, *anchors)
        frozen.append(
            tuple(
                (d, store[account][d])
                for d in DAYS
                if left <= d <= right and store[account][d]
            )
        )
    return tuple(frozen)


def signature(frozen, mutation=None):
    return tuple(
        tuple((d, content) for d, content in rows if mutation != "signature" or d >= 0)
        for rows in frozen
    )


def verify(mutation=None):
    count = 0
    for anchors in product(DAYS, repeat=2):
        for contents in product((0, 1, 2), repeat=6):
            store = [dict(zip(DAYS, contents[i : i + 3])) for i in (0, 3)]
            frozen = freeze(store, anchors, mutation)
            for account, anchor in enumerate(anchors):
                expected = tuple(
                    (d, store[account][d])
                    for d in DAYS
                    if min(0, anchor) <= d <= max(0, anchor) and store[account][d]
                )
                assert frozen[account] == expected, (
                    "necessary local dates/account isolation",
                    anchors,
                    store,
                )
            old_signature = signature(frozen, mutation)
            # Exhaust every one-source correction after freeze and publication.
            for account, day, value in product(range(2), DAYS, (1, 2)):
                changed = [s.copy() for s in store]
                changed[account][day] = value
                fresh = freeze(changed, anchors, mutation)
                restored = fresh if mutation == "restore" else frozen
                assert restored == frozen, "old frozen reads new export"
                if fresh != frozen:
                    assert signature(fresh, mutation) != old_signature, (
                        "relevant correction not detected"
                    )
                # Candidate preparation/failure never replaces the published immutable value.
                for outcome in ("failed", "superseded", "verified"):
                    published = (
                        fresh
                        if outcome == "verified" or mutation == "failed"
                        else frozen
                    )
                    assert published == (fresh if outcome == "verified" else frozen), (
                        "failed candidate overwrote publication"
                    )
                count += 1
    return count


if __name__ == "__main__":
    print(f"PASS {verify()} bounded source transitions")
    for mutation in ("boundary", "signature", "scope", "restore", "failed"):
        try:
            verify(mutation)
        except AssertionError as error:
            print(f"REJECT {mutation}: {error}")
        else:
            raise AssertionError(f"negative control survived: {mutation}")
