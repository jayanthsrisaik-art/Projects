import random

import pytest

from sketchroll.hll import HyperLogLog


@pytest.mark.parametrize("n", [0, 1, 7, 100, 1_000, 10_000, 60_000])
def test_estimate_within_four_standard_errors(n):
    h = HyperLogLog(12)
    for i in range(n):
        h.add(f"user-{n}-{i}")
    est = h.estimate()
    if n == 0:
        assert est == 0
    else:
        assert abs(est - n) / n <= 4 * h.relative_std_error


def test_adding_duplicates_does_not_change_sketch():
    a, b = HyperLogLog(10), HyperLogLog(10)
    for i in range(500):
        a.add(str(i))
        b.add(str(i))
        b.add(str(i))
    assert a == b


def test_merge_of_parts_equals_sketch_of_union_exactly():
    rng = random.Random(1)
    items = [f"x{rng.randrange(50_000)}" for _ in range(20_000)]
    whole, left, right = HyperLogLog(12), HyperLogLog(12), HyperLogLog(12)
    for i, x in enumerate(items):
        whole.add(x)
        (left if i % 3 else right).add(x)
    assert left.copy().merge(right) == whole
    assert right.copy().merge(left) == whole  # commutative


def test_merge_rejects_mismatched_precision():
    with pytest.raises(ValueError):
        HyperLogLog(10).merge(HyperLogLog(12))


def test_serialisation_roundtrip_and_corruption():
    h = HyperLogLog(12)
    for i in range(3000):
        h.add(str(i))
    blob = h.to_bytes()
    assert len(blob) < h.m  # compressed below raw register size at this cardinality
    assert HyperLogLog.from_bytes(blob) == h
    with pytest.raises(ValueError):
        HyperLogLog.from_bytes(b"XXX" + blob[3:])


def test_hash_is_stable_across_processes():
    from sketchroll.hll import hash64
    assert hash64("u000001") == hash64("u000001")
    # pinned value: blake2b-64 must not depend on PYTHONHASHSEED or platform
    assert hash64("u000001") == 1701409839342322472
