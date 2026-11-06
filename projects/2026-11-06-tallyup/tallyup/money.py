"""Exact money handling: decimal strings <-> integer minor units, and cent allocation.

Every amount inside tallyup is an ``int`` number of minor units (cents for USD,
yen for JPY, fils for KWD). Floats never touch money. Allocation splits an
integer total into integer parts that sum *exactly* to the total.
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Mapping, Sequence

# ISO 4217 minor-unit exponents for the currencies the demo supports.
CURRENCY_EXPONENT: dict[str, int] = {
    "USD": 2, "EUR": 2, "GBP": 2, "INR": 2, "CAD": 2, "AUD": 2,
    "JPY": 0, "KRW": 0,
    "KWD": 3, "BHD": 3,
}

_AMOUNT_RE = re.compile(r"^(-?)(\d{1,12})(?:\.(\d+))?$")
MAX_MINOR = 10**15


class MoneyError(ValueError):
    """Raised for amounts that cannot be represented exactly."""


def exponent_for(currency: str) -> int:
    try:
        return CURRENCY_EXPONENT[currency]
    except KeyError:
        raise MoneyError(f"unsupported currency {currency!r}") from None


def parse_amount(text: str, exponent: int, *, allow_negative: bool = False,
                 allow_zero: bool = False) -> int:
    """Parse a decimal string such as ``"12.30"`` into minor units.

    Rejects floats-in-disguise like ``"1e3"``, thousands separators, and more
    fractional digits than the currency has (``"0.001"`` USD is an error, not
    silently rounded).
    """
    if not isinstance(text, str):
        raise MoneyError("amount must be a decimal string, e.g. \"12.30\"")
    m = _AMOUNT_RE.match(text.strip())
    if not m:
        raise MoneyError(f"invalid amount {text!r}")
    sign, whole, frac = m.groups()
    frac = frac or ""
    if len(frac) > exponent:
        # Allow trailing zeros beyond the exponent ("1.50" for JPY is not ok, "5.000" for USD is).
        if frac[exponent:].strip("0"):
            raise MoneyError(
                f"amount {text!r} has more than {exponent} decimal places")
        frac = frac[:exponent]
    value = int(whole) * 10**exponent + (int(frac.ljust(exponent, "0")) if exponent else 0)
    if sign:
        value = -value
    if value < 0 and not allow_negative:
        raise MoneyError("amount must not be negative")
    if value == 0 and not allow_zero:
        raise MoneyError("amount must be greater than zero")
    if abs(value) > MAX_MINOR:
        raise MoneyError("amount too large")
    return value


def format_amount(minor: int, exponent: int) -> str:
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    if exponent == 0:
        return f"{sign}{minor}"
    whole, frac = divmod(minor, 10**exponent)
    return f"{sign}{whole}.{frac:0{exponent}d}"


def parse_fraction(text: str, *, max_places: int = 6) -> Fraction:
    """Parse a non-negative decimal string (percentages) into an exact Fraction."""
    if not isinstance(text, str):
        raise MoneyError("value must be a decimal string")
    m = _AMOUNT_RE.match(text.strip())
    if not m or m.group(1):
        raise MoneyError(f"invalid decimal {text!r}")
    frac = m.group(3) or ""
    if len(frac) > max_places:
        raise MoneyError(f"{text!r} has more than {max_places} decimal places")
    return Fraction(text.strip())


def allocate(total: int, weights: Sequence[Fraction | int],
             bias: Sequence[Fraction] | None = None) -> list[int]:
    """Split ``total`` minor units in proportion to ``weights``.

    Each part is ``floor(exact)`` or ``floor(exact) + 1`` so no part is ever a
    whole unit away from its exact share, and the parts sum to ``total``.

    Which members receive the leftover units is the only free choice:

    * ``bias is None`` -> classic largest-remainder (Hamilton) with ties broken
      by position. Deterministic, but the same member wins every tie forever.
    * ``bias`` given -> *drift-aware*: leftover units go to the members whose
      cumulative rounding bias (allocated - exact, summed over earlier
      expenses) would be lowest after receiving one. Over many expenses no
      member keeps paying the odd cent.
    """
    if total < 0:
        raise MoneyError("total must be non-negative")
    ws = [Fraction(w) for w in weights]
    if not ws:
        raise MoneyError("nothing to allocate to")
    if any(w < 0 for w in ws):
        raise MoneyError("weights must be non-negative")
    wsum = sum(ws)
    if wsum == 0:
        raise MoneyError("weights must not all be zero")
    exact = [total * w / wsum for w in ws]
    parts = [int(e.numerator // e.denominator) for e in exact]
    leftover = total - sum(parts)
    remainders = [e - p for e, p in zip(exact, parts)]
    # Only members with a fractional remainder may receive a leftover unit.
    candidates = [i for i, r in enumerate(remainders) if r > 0]
    if bias is None:
        order = sorted(candidates, key=lambda i: (-remainders[i], i))
    else:
        if len(bias) != len(ws):
            raise MoneyError("bias length mismatch")
        # Bias after receiving the unit is bias + (1 - r). Prefer the smallest;
        # then the larger remainder (closer to classic rounding); then position.
        order = sorted(candidates,
                       key=lambda i: (bias[i] + 1 - remainders[i], -remainders[i], i))
    for i in order[:leftover]:
        parts[i] += 1
    assert sum(parts) == total
    return parts


def rounding_bias(parts: Sequence[int], total: int,
                  weights: Sequence[Fraction | int]) -> list[Fraction]:
    """allocated - exact for each part (exact rational arithmetic)."""
    ws = [Fraction(w) for w in weights]
    wsum = sum(ws)
    return [Fraction(p) - total * w / wsum for p, w in zip(parts, ws)]
