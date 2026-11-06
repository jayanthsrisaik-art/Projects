import json
import random
from fractions import Fraction
from pathlib import Path

import pytest

from tallyup.money import MoneyError, allocate, format_amount, parse_amount, rounding_bias
from tallyup.splits import SplitError, compute_split

VECTORS = json.loads((Path(__file__).resolve().parent.parent / "samples" / "money_vectors.json").read_text())


@pytest.mark.parametrize("v", VECTORS, ids=lambda v: f"{v['text']!r}@{v['exponent']}")
def test_parse_amount_shared_vectors(v):
    if v.get("error"):
        with pytest.raises(MoneyError):
            parse_amount(v["text"], v["exponent"], allow_zero=v.get("allow_zero", False))
    else:
        assert parse_amount(v["text"], v["exponent"], allow_zero=v.get("allow_zero", False)) == v["minor"]


def test_parse_rejects_non_strings():
    with pytest.raises(MoneyError):
        parse_amount(12.3, 2)  # floats are refused, not rounded


@pytest.mark.parametrize("minor,exp,text", [(1230, 2, "12.30"), (5, 2, "0.05"), (1500, 0, "1500"),
                                            (1, 3, "0.001"), (-250, 2, "-2.50")])
def test_format_round_trip(minor, exp, text):
    assert format_amount(minor, exp) == text
    assert parse_amount(text, exp, allow_negative=True) == minor


def test_allocate_classic_examples():
    assert allocate(100, [1, 1, 1]) == [34, 33, 33]
    assert allocate(1000, [1, 1, 1]) == [334, 333, 333]
    assert allocate(1, [1, 1, 1]) == [1, 0, 0]
    assert allocate(0, [1, 2]) == [0, 0]
    assert allocate(10000, [Fraction("33.333"), Fraction("33.333"), Fraction("33.334")]) == [3333, 3333, 3334]


def test_allocate_properties_random():
    rng = random.Random(7)
    for _ in range(3000):
        n = rng.randint(1, 9)
        weights = [rng.randint(0, 5) for _ in range(n)]
        if sum(weights) == 0:
            weights[0] = 1
        total = rng.randint(0, 100_000)
        bias = [Fraction(rng.randint(-200, 200), 100) for _ in range(n)] if rng.random() < 0.5 else None
        parts = allocate(total, weights, bias)
        assert sum(parts) == total
        for p, b in zip(parts, rounding_bias(parts, total, weights)):
            assert p >= 0
            assert -1 < b < 1, "each part within one minor unit of its exact share"
        for p, w in zip(parts, weights):
            if w == 0:
                assert p == 0


def test_drift_aware_rotates_the_odd_cent():
    # Classic largest remainder gives member 0 the extra cent every time.
    bias = [Fraction(0)] * 3
    winners = []
    for _ in range(6):
        parts = allocate(100, [1, 1, 1], bias)
        winners.append(parts.index(34))
        bias = [b + d for b, d in zip(bias, rounding_bias(parts, 100, [1, 1, 1]))]
    assert winners == [0, 1, 2, 0, 1, 2]
    assert max(abs(b) for b in bias) < 1


def test_allocate_input_errors():
    with pytest.raises(MoneyError):
        allocate(10, [])
    with pytest.raises(MoneyError):
        allocate(10, [0, 0])
    with pytest.raises(MoneyError):
        allocate(-1, [1])


def test_compute_split_kinds():
    members = [1, 2, 3]
    eq = compute_split(1000, {"kind": "equal", "parts": [{"member_id": m} for m in members]}, 2, members)
    assert eq.shares == {1: 334, 2: 333, 3: 333}
    sh = compute_split(1000, {"kind": "shares", "parts": [{"member_id": 1, "value": 2}, {"member_id": 2, "value": "1"}]}, 2, members)
    assert sh.shares == {1: 667, 2: 333}
    pc = compute_split(999, {"kind": "percent", "parts": [{"member_id": 1, "value": "12.5"}, {"member_id": 2, "value": "87.5"}]}, 2, members)
    assert pc.shares == {1: 125, 2: 874} and sum(pc.shares.values()) == 999
    ex = compute_split(1000, {"kind": "exact", "parts": [{"member_id": 1, "value": "2.50"}, {"member_id": 3, "value": "7.50"}]}, 2, members)
    assert ex.shares == {1: 250, 3: 750}


@pytest.mark.parametrize("spec,err", [
    ({"kind": "bogus", "parts": [{"member_id": 1}]}, "split kind"),
    ({"kind": "equal", "parts": []}, "at least one"),
    ({"kind": "equal", "parts": [{"member_id": 9}]}, "not in this group"),
    ({"kind": "equal", "parts": [{"member_id": 1}, {"member_id": 1}]}, "twice"),
    ({"kind": "percent", "parts": [{"member_id": 1, "value": "50"}, {"member_id": 2, "value": "49.99"}]}, "exactly 100"),
    ({"kind": "exact", "parts": [{"member_id": 1, "value": "5.00"}, {"member_id": 2, "value": "4.99"}]}, "off by -1"),
    ({"kind": "shares", "parts": [{"member_id": 1, "value": 0}]}, "between 1"),
    ({"kind": "shares", "parts": [{"member_id": 1, "value": "1.5"}]}, "positive integers"),
    ({"kind": "exact", "parts": [{"member_id": 1, "value": "10.001"}]}, "decimal places"),
])
def test_compute_split_errors(spec, err):
    with pytest.raises(SplitError, match=err):
        compute_split(1000, spec, 2, [1, 2, 3])
