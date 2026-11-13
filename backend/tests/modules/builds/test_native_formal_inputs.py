"""R7: generated decimal inputs; oracle uses integer coefficients, not float/sum code."""

from decimal import Decimal
from itertools import product

from app.modules.builds.frozen_requests import native_batch_budget


def test_generated_decimal_budget():
    # Spec R7: each INCLUDED Campaign once, independent of ad count.
    # 1..18 decimal places, 1..3 plans, 0/1/7 ads. Exact coefficient oracle.
    for scale, count, ads in product(range(1, 19), range(1, 4), (0, 1, 7)):
        coefficients = [10**scale + 17 + i * 23 for i in range(count)]
        requests = [
            {
                "campaign": {"budget": f"{c // 10**scale}.{c % 10**scale:0{scale}d}"},
                "ads": [None] * ads,
            }
            for c in coefficients
        ]
        # Closed arithmetic-series oracle is independent of implementation's summation.
        expected_coefficient = count * (10**scale + 17) + 23 * count * (count - 1) // 2
        expected = Decimal((0, tuple(map(int, str(expected_coefficient))), -scale))
        assert native_batch_budget(requests) == expected
    assert native_batch_budget([]) == Decimal(0)
