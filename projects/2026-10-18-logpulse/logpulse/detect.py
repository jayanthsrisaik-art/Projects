"""Windowed anomaly detection: z-score baseline vs IsolationForest on engineered features."""
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score
from sklearn.preprocessing import StandardScaler

from .store import window_rows

FEATURES = ("log_n", "err_rate", "new_frac", "n_tpl", "lat_mean", "lat_max", "count_ratio")


def build_matrix(rows, lookback: int = 30):
    """Feature matrix + window ids. count_ratio compares to the median of the *previous*
    `lookback` windows only (causal), so it carries no future information."""
    wins = np.array([r["win"] for r in rows])
    n = np.array([r["n"] for r in rows], float)
    X = np.zeros((len(rows), len(FEATURES)))
    for i, r in enumerate(rows):
        past = n[max(0, i - lookback):i]
        ratio = n[i] / max(np.median(past), 1.0) if len(past) else 1.0
        X[i] = (np.log1p(n[i]), r["n_err"] / n[i], r["n_new"] / n[i], r["n_tpl"],
                np.log1p(r["lat_mean"] or 0.0), np.log1p(r["lat_max"] or 0.0), ratio)
    return wins, X


def prf(y_true, y_pred) -> dict:
    tp = int(np.sum(y_true & y_pred)); fp = int(np.sum(~y_true & y_pred)); fn = int(np.sum(y_true & ~y_pred))
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0, "tp": tp, "fp": fp, "fn": fn}


@dataclass
class Detection:
    windows: np.ndarray
    scores: np.ndarray   # higher = more anomalous
    flagged: np.ndarray  # bool
    split_win: int


def detect(con, train_fraction: float = 0.6, quantile: float = 0.99, seed: int = 0, method: str = "iforest"):
    """Fit on the first `train_fraction` of windows (chronological), score the rest.
    The alert threshold is the `quantile` of *training* scores. Returns test-period Detection."""
    # Pass 1: locate the split using timestamps only (no features yet).
    wins_all = [r[0] for r in con.execute("SELECT DISTINCT ts / 60 FROM logs ORDER BY 1")]
    split_idx = int(len(wins_all) * train_fraction)
    split_win = wins_all[split_idx]
    rows = window_rows(con, known_before=split_win * 60)
    wins, X = build_matrix(rows)
    tr = wins < split_win
    if method == "zscore":  # baseline: error-count z-score against training mean/std
        err = X[:, 1] * np.exp(X[:, 0])
        mu, sd = err[tr].mean(), err[tr].std() or 1.0
        score = (err - mu) / sd
        thr = 3.0
    else:
        scaler = StandardScaler().fit(X[tr])
        model = IsolationForest(n_estimators=200, random_state=seed).fit(scaler.transform(X[tr]))
        score = -model.score_samples(scaler.transform(X))
        thr = float(np.quantile(score[tr], quantile))
    te = ~tr
    return Detection(wins[te], score[te], score[te] > thr, split_win)


def evaluate(det: Detection, incident_windows: set[int], incidents_by_window: list[set[int]]) -> dict:
    y = np.array([w in incident_windows for w in det.windows])
    m = prf(y, det.flagged)
    m["average_precision"] = float(average_precision_score(y, det.scores)) if y.any() else 0.0
    test_inc = [s for s in incidents_by_window if s & set(det.windows.tolist())]
    hit = sum(1 for s in test_inc if any(det.flagged[i] for i, w in enumerate(det.windows) if w in s))
    m["incidents_in_test"] = len(test_inc)
    m["incidents_detected"] = hit
    m["test_windows"] = int(len(y))
    return m
