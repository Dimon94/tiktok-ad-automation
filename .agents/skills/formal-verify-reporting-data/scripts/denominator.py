"""Bounded R6 arithmetic check. Hand-calculated oracle, not a whole-system proof."""

import argparse
from decimal import Decimal as D

CASES = (
    ((D(10), D(90)), (D(20), D(90)), D("1.1")),
    ((D(10), D(90)), (D(20), None), D(".2")),
    ((D(10), None), (D(20), D(90)), None),
    ((D(0), D(0)), (D(20), D(90)), None),
    ((D(10), D(90)), (None, None), None),
    ((D(10), D(90)), (D(0), D(0)), D(0)),
)


def check(broken=False):
    for spend, revenue, expected in CASES:
        complete = all(x is not None for x in spend)
        denominator = sum((x for x in spend if x is not None), D(0))
        numerator = [x for x in revenue if x is not None]
        actual = (
            sum(numerator, D(0)) / denominator
            if numerator and denominator and (complete or broken)
            else None
        )
        assert actual == expected, (spend, revenue, actual, expected)
    print("R6 bounded denominator oracle: 6 cases PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broken-denominator", action="store_true")
    check(parser.parse_args().broken_denominator)
