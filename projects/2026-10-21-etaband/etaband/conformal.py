"""Interval predictors: split conformal (baseline), raw quantile GBM, and CQR."""
from __future__ import annotations

import math

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    """Finite-sample-corrected (1-alpha) empirical quantile: the ceil((n+1)(1-alpha))-th smallest score."""
    scores = np.asarray(scores, dtype=float)
    n = len(scores)
    if n == 0:
        raise ValueError("empty calibration scores")
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        return float("inf")
    return float(np.sort(scores)[k - 1])


class SplitConformalRidge:
    """Baseline: Ridge point forecast with a constant-width absolute-residual interval."""

    def __init__(self, alpha: float = 0.1):
        self.alpha = alpha
        self.model = make_pipeline(StandardScaler(), Ridge(alpha=1.0))

    def fit(self, X, y):
        self.model.fit(X, y)
        return self

    def calibrate(self, X, y):
        self.q_ = conformal_quantile(np.abs(np.asarray(y) - self.model.predict(X)), self.alpha)
        return self

    def predict_interval(self, X):
        mu = self.model.predict(X)
        return mu - self.q_, mu + self.q_

    def predict_point(self, X):
        return self.model.predict(X)


class QuantileGBM:
    """Lower/upper/median gradient-boosted quantile regressors; optional CQR calibration."""

    def __init__(self, alpha: float = 0.1, conformalize: bool = True, seed: int = 0, **gb):
        self.alpha, self.conformalize, self.seed = alpha, conformalize, seed
        params = dict(n_estimators=150, max_depth=3, learning_rate=0.08, subsample=0.8)
        params.update(gb)
        mk = lambda q: GradientBoostingRegressor(loss="quantile", alpha=q, random_state=seed, **params)
        self.lo, self.hi, self.mid = mk(alpha / 2), mk(1 - alpha / 2), mk(0.5)
        self.q_ = 0.0

    def fit(self, X, y):
        for m in (self.lo, self.hi, self.mid):
            m.fit(X, y)
        return self

    def calibrate(self, X, y):
        y = np.asarray(y)
        lo, hi = self.lo.predict(X), self.hi.predict(X)
        # CQR nonconformity: signed distance outside [lo, hi] (negative = inside)
        self.q_ = conformal_quantile(np.maximum(lo - y, y - hi), self.alpha) if self.conformalize else 0.0
        return self

    def predict_interval(self, X):
        lo, hi = self.lo.predict(X), self.hi.predict(X)
        lo, hi = np.minimum(lo, hi), np.maximum(lo, hi)  # fix quantile crossing
        return lo - self.q_, hi + self.q_

    def predict_point(self, X):
        return self.mid.predict(X)
