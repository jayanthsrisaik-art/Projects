import math
import random

import pytest

from sketchroll.ddsketch import DDSketch
from sketchroll.oracle import lower_quantile

QS = [0, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 0.999, 1]


@pytest.mark.parametrize("seed", range(5))
def test_relative_error_guarantee_on_heavy_tailed_data(seed):
    rng = random.Random(seed)
    data = [rng.lognormvariate(4, 1.5) for _ in range(5000)] + [rng.paretovariate(1.1) * 50 for _ in range(500)]
    s = DDSketch(alpha=0.01)
    for x in data:
        s.add(x)
    data.sort()
    for q in QS:
        true = lower_quantile(data, q)
        assert abs(s.quantile(q) - true) / true <= 0.01 + 1e-12, q


def test_zeros_and_tiny_values_go_to_zero_bucket():
    s = DDSketch()
    for x in [0.0, 0.0, 0.0, 5.0]:
        s.add(x)
    assert s.quantile(0.5) == 0.0
    assert abs(s.quantile(1.0) - 5.0) / 5.0 <= 0.01


def test_merge_equals_combined_sketch():
    rng = random.Random(3)
    data = [rng.expovariate(1 / 100) for _ in range(4000)]
    whole, a, b = DDSketch(), DDSketch(), DDSketch()
    for i, x in enumerate(data):
        whole.add(x)
        (a if i % 2 else b).add(x)
    assert a.copy().merge(b) == whole


def test_collapse_bounds_size_and_keeps_upper_quantiles():
    s = DDSketch(alpha=0.01, max_bins=100)
    data = [10 ** (i / 400) for i in range(4000)]  # spans 10 decades -> ~1150 natural bins
    for x in data:
        s.add(x)
    assert len(s.bins) <= 100 and s.collapsed
    assert s.count == len(data)
    for q in (0.99, 0.999, 1.0):
        true = lower_quantile(sorted(data), q)
        assert abs(s.quantile(q) - true) / true <= 0.01 + 1e-12


def test_invalid_inputs():
    s = DDSketch()
    for bad in (-1.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            s.add(bad)
    with pytest.raises(ValueError):
        s.quantile(0.5)  # empty
    s.add(1.0)
    with pytest.raises(ValueError):
        s.quantile(1.5)
    with pytest.raises(ValueError):
        s.merge(DDSketch(alpha=0.02))


def test_serialisation_roundtrip():
    s = DDSketch()
    for x in [0, 1.5, 2.5, 1000, 1000, 3e5]:
        s.add(x)
    r = DDSketch.from_bytes(s.to_bytes())
    assert r == s and r.quantile(0.5) == s.quantile(0.5)
