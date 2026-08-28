"""Money helpers.

Every amount in this project is an integer number of cents. The one place
floating point sneaks in is the two percentages on the inputs
(``settlement_pct``, ``program_fee_pct``), and ASSIGNMENT.md S3 is explicit
that those must round **half-up** (".5 always rounds away from zero") rather
than with Python's default half-to-even.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def _dec(value: Decimal | int | float | str) -> Decimal:
    # Decimal(str(x)) so 0.4 stays 0.4 rather than 0.4000000000000000222...
    return value if isinstance(value, Decimal) else Decimal(str(value))


def half_up(value: Decimal | int | float | str) -> int:
    """Round to the nearest integer, with .5 going away from zero."""
    return int(_dec(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pct_of_cents(pct: float, cents: int) -> int:
    """``round(pct * cents)`` with half-up rounding, in integer cents."""
    return half_up(_dec(pct) * Decimal(int(cents)))
