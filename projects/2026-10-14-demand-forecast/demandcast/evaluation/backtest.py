"""Rolling-origin backtest over all models, with intervals and segment breakdowns."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import ExperimentConfig
from ..data import generate_demand
from ..features import FeatureBuilder, FeatureSpec
from ..models import build_models
from ..models.quantile import QuantileInterval
from ..models.sklearn_models import DEFAULT_HGB_PARAMS
from .metrics import coverage, pinball, point_scores
from .splits import rolling_origin_splits
from .tuning import select_hgb_params

SEGMENTS = {
    "weekday": lambda X: (X["is_weekend"] == 0) & (X["is_holiday"] == 0),
    "weekend_or_holiday": lambda X: (X["is_weekend"] == 1) | (X["is_holiday"] == 1),
}


def _mean_std(values: list[float]) -> dict:
    a = np.asarray(values, dtype=float)
    return {"mean": round(float(a.mean()), 4), "std": round(float(a.std(ddof=0)), 4)}


def run_backtest(cfg: ExperimentConfig, df: pd.DataFrame | None = None) -> dict:
    """Run the full experiment and return a JSON-serialisable report."""
    df = generate_demand(cfg.days, cfg.seed, temp_noise_std=cfg.temp_noise_std) if df is None else df
    builder = FeatureBuilder(FeatureSpec(horizon=cfg.horizon))
    X, y = builder.transform_xy(df)
    ts = df.loc[X.index, "timestamp"].reset_index(drop=True)
    X, y = X.reset_index(drop=True), y.reset_index(drop=True)

    val_rows = cfg.val_days * 24
    folds = rolling_origin_splits(len(X), cfg.n_folds, cfg.test_days * 24, cfg.gap_hours, min_train=2 * val_rows)

    per_fold: list[dict] = []
    pooled = {"y": [], "X": [], "pred": {}}
    interval_rows = []
    for i, f in enumerate(folds):
        X_tr, y_tr = X.iloc[: f.train_end], y.iloc[: f.train_end]
        X_te, y_te = X.iloc[f.test_start : f.test_end], y.iloc[f.test_start : f.test_end]
        if not ts.iloc[f.train_end - 1] < ts.iloc[f.test_start]:  # guard, never expected to fail
            raise AssertionError("training data overlaps the test window")

        hgb_params = select_hgb_params(X_tr, y_tr, val_rows, cfg.seed) if cfg.tune else dict(DEFAULT_HGB_PARAMS)
        models = build_models(cfg.seed, hgb_params)
        preds = {}
        for name, model in models.items():
            preds[name] = model.fit(X_tr, y_tr).predict(X_te)

        interval = QuantileInterval(cfg.quantiles, cfg.seed, **hgb_params).fit(X_tr, y_tr)
        lo, hi = interval.predict_interval(X_te)
        lo_q, hi_q = cfg.quantiles
        interval_metrics = {
            "coverage": coverage(y_te, lo, hi),
            "mean_width": float(np.mean(hi - lo)),
            "pinball_lo": pinball(y_te, lo, lo_q),
            "pinball_hi": pinball(y_te, hi, hi_q),
        }
        interval_rows.append(interval_metrics)

        per_fold.append(
            {
                "fold": i,
                "train_rows": int(len(X_tr)),
                "test_rows": int(len(X_te)),
                "train_end": str(ts.iloc[f.train_end - 1]),
                "test_start": str(ts.iloc[f.test_start]),
                "test_end": str(ts.iloc[f.test_end - 1]),
                "hgb_params": hgb_params,
                "metrics": {n: point_scores(y_te, p) for n, p in preds.items()},
                "interval": interval_metrics,
            }
        )
        pooled["y"].append(y_te)
        pooled["X"].append(X_te)
        for n, p in preds.items():
            pooled["pred"].setdefault(n, []).append(p)

    names = list(per_fold[0]["metrics"])
    summary = {
        n: {m: _mean_std([fd["metrics"][n][m] for fd in per_fold]) for m in ("mae", "rmse", "wape")} for n in names
    }
    interval_summary = {k: _mean_std([r[k] for r in interval_rows]) for k in interval_rows[0]}

    y_all = pd.concat(pooled["y"], ignore_index=True)
    X_all = pd.concat(pooled["X"], ignore_index=True)
    segments = {}
    for seg, sel in SEGMENTS.items():
        mask = sel(X_all).to_numpy()
        segments[seg] = {
            "rows": int(mask.sum()),
            **{n: round(point_scores(y_all[mask], np.concatenate(pooled["pred"][n])[mask])["wape"], 4) for n in names},
        }

    best = min(summary, key=lambda n: summary[n]["mae"]["mean"])
    return {
        "config": cfg.to_dict(),
        "feature_warmup_rows": builder.warmup,
        "folds": per_fold,
        "summary": summary,
        "best_model_by_mae": best,
        "interval_summary": interval_summary,
        "segment_wape": segments,
    }
