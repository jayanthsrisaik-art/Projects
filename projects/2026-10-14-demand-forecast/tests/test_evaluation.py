import numpy as np
import pytest

from demandcast.data import generate_demand
from demandcast.evaluation import coverage, mae, pinball, rmse, rolling_origin_splits, wape
from demandcast.evaluation.splits import chronological_split
from demandcast.evaluation.tuning import select_hgb_params
from demandcast.features import FeatureBuilder


def test_metrics_known_values():
    y, p = [10, 20], [12, 16]
    assert mae(y, p) == 3.0
    assert rmse(y, p) == pytest.approx(np.sqrt(10))
    assert wape(y, p) == pytest.approx(0.2)
    assert pinball([10], [8], 0.9) == pytest.approx(1.8)   # under-prediction penalised by q
    assert pinball([10], [12], 0.9) == pytest.approx(0.2)  # over-prediction by 1-q
    assert coverage([1, 5, 9], [0, 0, 0], [2, 4, 10]) == pytest.approx(2 / 3)
    with pytest.raises(ValueError):
        wape([0, 0], [1, 1])


def test_rolling_splits_are_ordered_and_disjoint():
    folds = rolling_origin_splits(1000, n_folds=3, test_size=100, gap=10)
    assert folds[-1].test_end == 1000
    for a, b in zip(folds, folds[1:]):
        assert a.test_end == b.test_start and a.train_end < b.train_end
    for f in folds:
        assert f.train_end + 10 == f.test_start
    with pytest.raises(ValueError):
        rolling_origin_splits(100, 3, 50)


def test_chronological_split_bounds():
    assert chronological_split(100, 0.2) == 80
    with pytest.raises(ValueError):
        chronological_split(100, 1.0)


def test_tuning_uses_only_given_rows_and_is_deterministic():
    df = generate_demand(80, seed=5)
    X, y = FeatureBuilder().transform_xy(df)
    grid = {"learning_rate": (0.05, 0.1), "max_iter": (50,)}
    p1 = select_hgb_params(X, y, 14 * 24, 0, grid)
    assert p1 == select_hgb_params(X, y, 14 * 24, 0, grid)
    assert set(p1) == set(grid)
    with pytest.raises(ValueError):
        select_hgb_params(X, y, len(X), 0, grid)
