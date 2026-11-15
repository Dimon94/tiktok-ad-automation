"""Finite R2/R5/R6/R8 exploration, with independent rational expectations.

Run via guarded runner for product imports. No database or provider calls here.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import product
from zoneinfo import ZoneInfo

import app.models  # noqa: F401 - canonical model registration before service imports
from app.modules.reporting.dimension_query import merge_metric_buckets, metric_vector
from app.modules.reporting.metric_catalog import NATIVE_CONTRACT, semantics_for

STATES = (None, "DASH", "ABSENT", "0", "0.1", "90")


def number(raw):
    return None if raw in (None, "DASH", "ABSENT") else Decimal(raw)


def run():
    arithmetic = 0
    for raw in product(STATES, repeat=4):
        spends = [number(x) for x in raw[:2]]
        revenues = [number(x) for x in raw[2:]]
        vectors = [
            metric_vector(
                {
                    "spend": s,
                    "revenue_total": r,
                    "level": "campaign",
                    "source_kind": "PLATFORM_DIRECT",
                    "contract_revision": NATIVE_CONTRACT,
                },
                currency="USD",
                timezone="UTC",
            )
            for s, r in zip(spends, revenues, strict=True)
        ]
        actual = merge_metric_buckets(
            [((v,), False) for v in vectors], strict_denominators=True
        )[0]
        known = [Fraction(r) for r in revenues if r is not None]
        expected = (
            sum(known) / sum(Fraction(s) for s in spends)
            if known and all(s is not None for s in spends) and any(spends)
            else None
        )
        value = actual.values["roas_total"]
        assert (value is None) == (expected is None), raw
        if expected is not None:
            assert abs(Fraction(value) - expected) < Fraction(1, 10**24), raw
        assert (
            actual.values["revenue_total"] is None
            if not known
            else actual.values["revenue_total"]
            == sum(Decimal(r) for r in revenues if r is not None)
        )
        arithmetic += 1
    # One-coordinate-at-a-time partition: no invented window input exists on vectors.
    base = {
        "level": "campaign",
        "source_kind": "PLATFORM_DIRECT",
        "contract_revision": NATIVE_CONTRACT,
    }
    partitions = 0
    for field, options in (
        ("level", ("campaign", "adgroup", "ad")),
        ("source_kind", ("PLATFORM_DIRECT", "CHILD_DERIVED")),
        ("contract_revision", (NATIVE_CONTRACT, "LEGACY_UNKNOWN")),
        ("currency", ("USD", "EUR")),
    ):
        for option in options:
            key = base | {field: option}
            currency = key.pop("currency", "USD")
            a = metric_vector(
                base | {"spend": Decimal(50), "revenue_total": Decimal(40)},
                currency="USD",
                timezone="UTC",
            )
            b = metric_vector(
                key | {"spend": Decimal(5), "revenue_total": Decimal(0)},
                currency=currency,
                timezone="Asia/Hong_Kong",
            )
            buckets = merge_metric_buckets(
                [((a,), False), ((b,), False)], strict_denominators=True
            )
            same = option == ("USD" if field == "currency" else base[field])
            assert len(buckets) == (1 if same else 2)
            if same:
                assert buckets[0].timezone == "MIXED"
            partitions += 1
    # R8 is an interpretation boundary, not event attribution/liveness proof.
    d0 = semantics_for("revenue_d0", NATIVE_CONTRACT)
    total = semantics_for("revenue_total", NATIVE_CONTRACT)
    assert (d0.window, d0.click_hours, d0.traffic_scope) == (
        "CLICK_HOURS",
        24,
        "UNCONFIRMED",
    )
    assert (total.window, total.click_hours) == ("TOTAL_CUTOFF_UNKNOWN", None)
    for zone, hour, minute in product(
        ("UTC", "Asia/Hong_Kong", "America/Los_Angeles"), (0, 23), (10, 50)
    ):
        click = datetime(2026, 11, 13, hour, minute, tzinfo=ZoneInfo(zone))
        returned = click + timedelta(minutes=20)
        assert returned < click + timedelta(hours=d0.click_hours)
        if hour == 23 and minute == 50:
            assert returned.date() != click.date()
    print(
        f"R2/R6 arithmetic={arithmetic}; R5/R6 coordinate partitions={partitions}; R8 local-click cases=12 PASS"
    )


if __name__ == "__main__":
    run()
