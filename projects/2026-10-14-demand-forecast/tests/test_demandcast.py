import numpy as np
import pandas as pd
import pytest

from demandcast.data import generate_demand
from demandcast.evaluate import chronological_split, mae, rmse, run_experiment, wape
from demandcast.features import HORIZON, build_features, drop_warmup


def test_generator_deterministic_and_shaped():
    a, b = generate_demand(10, seed=1), generate_demand(10, seed=1)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 240 and (a["demand"] >= 0).all()
    assert not a.equals(generate_demand(10, seed=2))


def test_generator_rejects_bad_days():
    with pytest.raises(ValueError):
        generate_demand(0)


def test_features_do_not_see_recent_demand():
    """Changing demand within the last HORIZON hours must not change features at t."""
    df = generate_demand(30, seed=3)
    base = build_features(df)
    t = 500
    mod = df.copy()
    mod.loc[t - HORIZON + 1 : t + 200, "demand"] += 1000  # strictly newer than allowed
    changed = build_features(mod)
    pd.testing.assert_frame_equal(base.iloc[: t + 1], changed.iloc[: t + 1])


def test_features_do_use_old_demand():
    df = generate_demand(30, seed=3)
    mod = df.copy()
    mod.loc[100, "demand"] += 1000
    f0, f1 = build_features(df), build_features(mod)
    assert f1.loc[124, "lag_24"] == f0.loc[124, "lag_24"] + 1000


def test_unsorted_input_rejected():
    df = generate_demand(3).iloc[::-1]
    with pytest.raises(ValueError):
        build_features(df)


def test_drop_warmup_removes_nan_rows():
    df = generate_demand(14)
    X, y = drop_warmup(build_features(df), df["demand"])
    assert not X.isna().any().any() and len(X) == len(y) == len(df) - 191


def test_chronological_split_order():
    df = generate_demand(20)
    tr, te = chronological_split(df, 0.25)
    assert tr["timestamp"].max() < te["timestamp"].min()
    assert len(tr) + len(te) == len(df)
    with pytest.raises(ValueError):
        chronological_split(df, 1.0)


def test_metrics_known_values():
    y, p = [10, 20], [12, 16]
    assert mae(y, p) == 3.0
    assert rmse(y, p) == pytest.approx(np.sqrt(10))
    assert wape(y, p) == pytest.approx(6 / 30)


def test_experiment_reproducible_and_beats_baselines():
    r1 = run_experiment(days=120, seed=7)
    r2 = run_experiment(days=120, seed=7)
    assert r1 == r2
    m = r1["metrics"]
    assert r1["train_end"] < r1["test_start"]
    assert m["hist_gradient_boosting"]["mae"] < m["seasonal_naive_168h"]["mae"]
    assert m["hist_gradient_boosting"]["mae"] < m["seasonal_naive_24h"]["mae"]
