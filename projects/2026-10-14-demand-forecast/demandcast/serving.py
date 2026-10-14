"""Fit a final model on all history, persist it, and score new rows.

The artifact is a joblib pickle: only load files you created yourself.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import pandas as pd

from .config import ExperimentConfig
from .evaluation.tuning import select_hgb_params
from .features import FeatureBuilder, FeatureSpec
from .models.quantile import QuantileInterval
from .models.sklearn_models import DEFAULT_HGB_PARAMS, HGBForecaster


def train_final(df: pd.DataFrame, cfg: ExperimentConfig) -> dict:
    builder = FeatureBuilder(FeatureSpec(horizon=cfg.horizon))
    X, y = builder.transform_xy(df)
    params = select_hgb_params(X, y, cfg.val_days * 24, cfg.seed) if cfg.tune else dict(DEFAULT_HGB_PARAMS)
    return {
        "spec": builder.spec,
        "config": cfg.to_dict(),
        "hgb_params": params,
        "point": HGBForecaster(cfg.seed, **params).fit(X, y),
        "interval": QuantileInterval(cfg.quantiles, cfg.seed, **params).fit(X, y),
        "trained_through": str(df["timestamp"].iloc[-1]),
    }


def save_model(artifact: dict, path: str | Path) -> None:
    joblib.dump(artifact, path)


def load_model(path: str | Path) -> dict:
    return joblib.load(path)


def predict_frame(artifact: dict, df: pd.DataFrame) -> pd.DataFrame:
    """Score every row that has a full feature history; warm-up rows are skipped."""
    builder = FeatureBuilder(artifact["spec"])
    X, _ = builder.transform_xy(df)
    lo, hi = artifact["interval"].predict_interval(X)
    return pd.DataFrame(
        {
            "timestamp": df.loc[X.index, "timestamp"].to_numpy(),
            "prediction": artifact["point"].predict(X).round(2),
            "lower": lo.round(2),
            "upper": hi.round(2),
        }
    )
