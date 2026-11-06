"""Turn a split specification into exact per-member shares."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping

from .money import MoneyError, allocate, parse_amount, parse_fraction, rounding_bias

SPLIT_KINDS = ("equal", "shares", "percent", "exact")


class SplitError(ValueError):
    pass


@dataclass(frozen=True)
class SplitResult:
    shares: dict[int, int]          # member_id -> minor units owed
    bias: dict[int, Fraction]       # member_id -> allocated - exact


def compute_split(total: int, spec: Mapping[str, Any], exponent: int,
                  valid_members: Iterable[int],
                  prior_bias: Mapping[int, Fraction] | None = None) -> SplitResult:
    """Validate ``spec`` and allocate ``total`` across its members.

    ``prior_bias`` (member -> cumulative rounding bias in this group) switches
    the leftover-unit choice to drift-aware mode; ``None`` uses plain
    largest-remainder.
    """
    kind = spec.get("kind")
    if kind not in SPLIT_KINDS:
        raise SplitError(f"split kind must be one of {', '.join(SPLIT_KINDS)}")
    parts = spec.get("parts")
    if not isinstance(parts, list) or not parts:
        raise SplitError("split needs at least one part")
    valid = set(valid_members)
    ids: list[int] = []
    values: list[Any] = []
    for p in parts:
        mid = p.get("member_id") if isinstance(p, Mapping) else None
        if not isinstance(mid, int) or isinstance(mid, bool):
            raise SplitError("each part needs an integer member_id")
        if mid not in valid:
            raise SplitError(f"member {mid} is not in this group")
        if mid in ids:
            raise SplitError(f"member {mid} appears twice in the split")
        ids.append(mid)
        values.append(p.get("value"))

    try:
        if kind == "exact":
            amounts = [parse_amount(v, exponent, allow_zero=True) for v in values]
            if sum(amounts) != total:
                raise SplitError("exact amounts must add up to the expense amount "
                                 f"(off by {sum(amounts) - total} minor units)")
            return SplitResult(dict(zip(ids, amounts)), {m: Fraction(0) for m in ids})
        if kind == "equal":
            weights: list[Fraction] = [Fraction(1)] * len(ids)
        elif kind == "shares":
            weights = []
            for v in values:
                if isinstance(v, bool) or not isinstance(v, (int, str)):
                    raise SplitError("shares must be positive integers")
                try:
                    n = int(v)
                except ValueError:
                    raise SplitError("shares must be positive integers") from None
                if n <= 0 or n > 10_000:
                    raise SplitError("shares must be between 1 and 10000")
                weights.append(Fraction(n))
        else:  # percent
            weights = [parse_fraction(v) for v in values]
            if sum(weights) != 100:
                raise SplitError(f"percentages must add up to exactly 100 (got {float(sum(weights)):g})")
            if any(w == 0 for w in weights):
                raise SplitError("remove members with 0% instead of listing them")
    except MoneyError as e:
        raise SplitError(str(e)) from None

    bias_list = None
    if prior_bias is not None:
        bias_list = [prior_bias.get(m, Fraction(0)) for m in ids]
    alloc = allocate(total, weights, bias_list)
    b = rounding_bias(alloc, total, weights)
    return SplitResult(dict(zip(ids, alloc)), dict(zip(ids, b)))
