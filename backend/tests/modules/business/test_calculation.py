from datetime import date
from decimal import Decimal as D

import pytest


def test_financial_values_keep_precision_and_unknowns():
    from app.modules.business.calculation import business_amounts

    result = business_amounts(D("100"), D("123.45"), D("0.08"), D("0.06"))
    assert result["business_revenue"] == D("119.574")
    assert result["provider_share"] == D("9.876")
    assert business_amounts(D("100"), None, D(0), D(0))["business_revenue"] is None


@pytest.mark.parametrize(
    "spend,expected",
    [
        ("999.99", (0, 0, 0)),
        ("1000", (0, 0, 200)),
        ("2000", (800, 500, 400)),
        ("5000", (2000, 1250, 1000)),
        ("10000", (4000, 2500, 2000)),
        ("30000", (9000, 7500, 2000)),
    ],
)
def test_three_independent_tiers(spend, expected):
    from app.modules.business.calculation import tier_amounts

    assert tier_amounts(D(spend), date(2026, 11, 3), "USD") == expected


def test_policy_does_not_repeat_into_future_quarters_or_other_currencies():
    from app.modules.business.calculation import tier_amounts

    assert tier_amounts(D(30000), date(2027, 1, 1), "USD") == (None,) * 3
    assert tier_amounts(D(30000), date(2026, 11, 3), "EUR") == (None,) * 3


def test_compensation_strict_trigger_and_missing_evidence():
    from app.modules.business.calculation import compensation_amount

    assert compensation_amount(D(120), D(100), D(1), D(300), True) == 0
    assert compensation_amount(D(121), D(100), D(1), D(300), True) == 21
    assert compensation_amount(D(121), D(100), D(1), D(299), True) == 0
    assert compensation_amount(D(121), D(100), D(1), D(300), None) is None
    assert compensation_amount(D(121), D(100), None, D(300), True) is None


def test_allocation_conserves_total_including_negative_revision():
    from app.modules.business.calculation import allocate_amount

    for amount in (D(800), D(-300), D("0.01")):
        parts = allocate_amount(amount, {"a": D(1), "b": D(2), "c": D(3)})
        assert sum(parts.values()) == amount
    assert allocate_amount(D(0), {"a": D(0)}) == {"a": D(0)}
