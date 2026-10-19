"""Integer micro-USD helpers; all conversions round reserve amounts upward."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

MICRO_USD = 1_000_000


def usd_to_micro(value: str | int | Decimal) -> int:
    return int((Decimal(str(value)) * MICRO_USD).quantize(Decimal(1), rounding=ROUND_CEILING))


def multiply_ceil(amount_micro: int, multiplier: Decimal | str) -> int:
    return int(
        (Decimal(amount_micro) * Decimal(multiplier)).quantize(Decimal(1), rounding=ROUND_CEILING)
    )
