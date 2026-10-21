import numpy as np
import pytest

from etaband.conformal import conformal_quantile
from etaband.data import generate_orders, time_split
from etaband.metrics import coverage, interval_score, segment_coverage
from etaband.pipeline import run


def test_conformal_quantile_rank():
    s = np.arange(1, 10)  # n=9, alpha=.2 -> k=ceil(10*.8)=8
    assert conformal_quantile(s, 0.2) == 8
    assert conformal_quantile(s, 0.01) == float("inf")  # too few points for that level
    with pytest.raises(ValueError):
        conformal_quantile([], 0.1)


def test_time_split_is_chronological_and_disjoint():
    df = generate_orders(n=3000, seed=1)
    tr, ca, te = time_split(df.sample(frac=1, random_state=0))
    assert tr.ts_min.max() <= ca.ts_min.min() <= ca.ts_min.max() <= te.ts_min.min()
    assert len(tr) + len(ca) + len(te) == len(df)
    with pytest.raises(ValueError):
        time_split(df, 0.8, 0.3)


def test_generator_deterministic_and_positive():
    a, b = generate_orders(2000, seed=3), generate_orders(2000, seed=3)
    assert a.equals(b)
    assert (a.minutes > 0).all()


def test_metrics():
    y = np.array([1.0, 5.0, 10.0])
    lo, hi = np.array([0.0, 0.0, 0.0]), np.array([2.0, 2.0, 2.0])
    assert coverage(y, lo, hi) == pytest.approx(1 / 3)
    # width 2 each; misses: 3 and 8 -> penalty (2/0.1)*(3+8)/3 added to mean width 2
    assert interval_score(y, lo, hi, 0.1) == pytest.approx(2 + 20 * 11 / 3)
    assert segment_coverage(y, lo, hi, ["a", "b", "b"]) == {"a": 1.0, "b": 0.0}


def test_run_reproducible_and_calibrated():
    r1, r2 = run(n=9000, seed=2), run(n=9000, seed=2)
    assert r1 == r2
    m = r1["models"]
    # conformal methods hit nominal coverage within sampling noise (test n ~ 700)
    assert abs(m["cqr_gbm"]["coverage"] - 0.9) < 0.04
    assert abs(m["ridge_split_conformal"]["coverage"] - 0.9) < 0.04
    # heteroscedastic data: adaptive intervals beat constant width on interval score and MAE
    assert m["cqr_gbm"]["interval_score"] < m["ridge_split_conformal"]["interval_score"]
    assert m["cqr_gbm"]["point_mae_min"] < m["ridge_split_conformal"]["point_mae_min"]
    assert m["cqr_gbm"]["worst_segment_coverage"] > m["ridge_split_conformal"]["worst_segment_coverage"]
