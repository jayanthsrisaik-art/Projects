import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from .sklearn_models import DEFAULT_HGB_PARAMS


class QuantileInterval:
    """Prediction interval from two gradient-boosting quantile regressors."""

    def __init__(self, quantiles: tuple[float, float] = (0.1, 0.9), seed: int = 0, **params):
        self.quantiles = quantiles
        p = {**DEFAULT_HGB_PARAMS, **params}
        self.lo = HistGradientBoostingRegressor(loss="quantile", quantile=quantiles[0], random_state=seed, **p)
        self.hi = HistGradientBoostingRegressor(loss="quantile", quantile=quantiles[1], random_state=seed, **p)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "QuantileInterval":
        self.lo.fit(X, y)
        self.hi.fit(X, y)
        return self

    def predict_interval(self, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        a, b = self.lo.predict(X), self.hi.predict(X)
        # independent models can cross; enforce lower <= upper and non-negative counts
        return np.clip(np.minimum(a, b), 0, None), np.clip(np.maximum(a, b), 0, None)
