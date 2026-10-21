"""End-to-end experiment: generate -> time split -> fit -> calibrate -> evaluate on held-out future."""
from __future__ import annotations

import numpy as np

from .conformal import QuantileGBM, SplitConformalRidge
from .data import FEATURES, generate_orders, time_split
from .metrics import coverage, interval_score, mae, mean_width, segment_coverage


def segment_labels(df):
    return np.where(df["is_rain"] == 1, "rain", np.where(df["hour"].isin([12, 13, 19, 20]), "rush", "normal"))


def run(n: int = 12000, seed: int = 0, alpha: float = 0.1) -> dict:
    df = generate_orders(n=n, seed=seed)
    train, calib, test = time_split(df)
    # splits are strictly ordered in time and disjoint
    assert train.ts_min.max() <= calib.ts_min.min() and calib.ts_min.max() <= test.ts_min.min()
    Xtr, ytr = train[FEATURES], train["minutes"]
    Xca, yca = calib[FEATURES], calib["minutes"]
    Xte, yte = test[FEATURES], test["minutes"]
    models = {
        "ridge_split_conformal": SplitConformalRidge(alpha),
        "quantile_gbm_raw": QuantileGBM(alpha, conformalize=False, seed=seed),
        "cqr_gbm": QuantileGBM(alpha, conformalize=True, seed=seed),
    }
    seg = segment_labels(test)
    out = {"config": {"n_orders": len(df), "seed": seed, "alpha": alpha, "target_coverage": 1 - alpha,
                      "split_sizes": [len(train), len(calib), len(test)]}, "models": {}}
    for name, m in models.items():
        m.fit(Xtr, ytr).calibrate(Xca, yca)
        lo, hi = m.predict_interval(Xte)
        sc = segment_coverage(yte, lo, hi, seg)
        out["models"][name] = {
            "coverage": round(coverage(yte, lo, hi), 4),
            "mean_width_min": round(mean_width(lo, hi), 3),
            "interval_score": round(interval_score(yte, lo, hi, alpha), 3),
            "point_mae_min": round(mae(yte, m.predict_point(Xte)), 3),
            "segment_coverage": {k: round(v, 4) for k, v in sc.items()},
            "worst_segment_coverage": round(min(sc.values()), 4),
        }
    return out
