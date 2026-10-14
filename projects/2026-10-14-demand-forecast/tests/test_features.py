import pandas as pd
import pytest

from demandcast.data import generate_demand
from demandcast.features import FeatureBuilder, FeatureSpec


def test_features_do_not_see_recent_demand():
    """Changing demand inside the forbidden horizon must not change features at or before t."""
    df = generate_demand(40, seed=3)
    fb = FeatureBuilder()
    base = fb.transform(df)
    t = 700
    mod = df.copy()
    mod.loc[t - fb.spec.horizon + 1 :, "demand"] += 1000
    pd.testing.assert_frame_equal(base.iloc[: t + 1], fb.transform(mod).iloc[: t + 1])


@pytest.mark.parametrize("horizon", [1, 24, 48])
def test_no_leakage_for_other_horizons(horizon):
    df = generate_demand(40, seed=3)
    fb = FeatureBuilder(FeatureSpec(horizon=horizon))
    t = 700
    mod = df.copy()
    mod.loc[t - horizon + 1 :, "demand"] += 1000
    pd.testing.assert_frame_equal(fb.transform(df).iloc[: t + 1], fb.transform(mod).iloc[: t + 1])


def test_features_use_old_demand():
    df = generate_demand(40, seed=3)
    mod = df.copy()
    mod.loc[100, "demand"] += 1000
    fb = FeatureBuilder()
    assert fb.transform(mod).loc[124, "lag_24"] == fb.transform(df).loc[124, "lag_24"] + 1000


def test_same_hour_mean_value():
    df = generate_demand(40, seed=3)
    f = FeatureBuilder().transform(df)
    t = 600
    expected = df["demand"].iloc[[t - 168, t - 336, t - 504]].mean()
    assert f.loc[t, "same_hour_mean_3w"] == pytest.approx(expected)


def test_warmup_is_exact():
    df = generate_demand(40, seed=3)
    fb = FeatureBuilder()
    feats = fb.transform(df)
    nan_rows = feats.isna().any(axis=1)
    assert nan_rows.iloc[: fb.warmup].all() and not nan_rows.iloc[fb.warmup :].any()
    X, y = fb.transform_xy(df)
    assert len(X) == len(y) == len(df) - fb.warmup


def test_input_validation():
    df = generate_demand(5)
    fb = FeatureBuilder()
    with pytest.raises(ValueError, match="sorted"):
        fb.transform(df.iloc[::-1])
    with pytest.raises(ValueError, match="gap-free"):
        fb.transform(df.drop(index=10).reset_index(drop=True))
    with pytest.raises(ValueError, match="missing"):
        fb.transform(df.drop(columns=["temp_forecast"]))
