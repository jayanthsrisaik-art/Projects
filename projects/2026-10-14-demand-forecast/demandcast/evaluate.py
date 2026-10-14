"""Chronological splitting, metrics and the experiment runner."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.compose import ColumnTransformer

from .data import generate_demand
from .features import HORIZON, build_features, drop_warmup


def chronological_split(df: pd.DataFrame, test_frac: float = 0.2):
    """Split by time (no shuffling). Test is the final `test_frac` of rows."""
    if not 0 < test_frac < 1:
        raise ValueError("test_frac must be in (0, 1)")
    cut = int(len(df) * (1 - test_frac))
    return df.iloc[:cut], df.iloc[cut:]


def mae(y, p) -> float:
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(p))))


def rmse(y, p) -> float:
    return float(np.sqrt(np.mean((np.asarray(y) - np.asarray(p)) ** 2)))


def wape(y, p) -> float:
    y = np.asarray(y, dtype=float)
    return float(np.abs(y - np.asarray(p)).sum() / y.sum())


def _scores(y, p) -> dict:
    return {"mae": round(mae(y, p), 3), "rmse": round(rmse(y, p), 3), "wape": round(wape(y, p), 4)}


def _ridge():
    pre = ColumnTransformer(
        [("cat", OneHotEncoder(handle_unknown="ignore"), ["hour", "dayofweek"])],
        remainder=StandardScaler(),
    )
    return make_pipeline(pre, Ridge(alpha=1.0))


def run_experiment(days: int = 365, seed: int = 42, test_frac: float = 0.2) -> dict:
    """Train on the past, evaluate on the final `test_frac` of the timeline.

    Features are built on the full series; this is leakage-safe because every
    demand-derived feature only looks back >= HORIZON hours (see tests).
    """
    df = generate_demand(days=days, seed=seed)
    feats = build_features(df)
    X, y = drop_warmup(feats, df["demand"])
    train_idx, test_idx = chronological_split(X, test_frac)
    X_tr, y_tr = X.loc[train_idx.index], y.loc[train_idx.index]
    X_te, y_te = X.loc[test_idx.index], y.loc[test_idx.index]

    results = {
        "seasonal_naive_24h": _scores(y_te, X_te["lag_24"]),
        "seasonal_naive_168h": _scores(y_te, X_te["lag_168"]),
    }
    ridge = _ridge().fit(X_tr, y_tr)
    results["ridge"] = _scores(y_te, ridge.predict(X_te))
    gbm = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=seed)
    gbm.fit(X_tr, y_tr)
    results["hist_gradient_boosting"] = _scores(y_te, gbm.predict(X_te))

    return {
        "config": {"days": days, "seed": seed, "test_frac": test_frac, "horizon_hours": HORIZON},
        "n_train": int(len(X_tr)),
        "n_test": int(len(X_te)),
        "train_end": str(df.loc[X_tr.index[-1], "timestamp"]),
        "test_start": str(df.loc[X_te.index[0], "timestamp"]),
        "metrics": results,
    }


def save_report(report: dict, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "metrics.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    return path
