"""Targeting scorers: two baselines and three uplift learners.

Every scorer exposes ``fit(X, t, y)`` and ``score(X)``; a higher score means
"contact this customer first". Only the uplift learners' scores are estimates
of the treatment effect P(y=1|x,T=1) - P(y=1|x,T=0).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor


def _clf(seed: int) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=200,
        learning_rate=0.05,
        max_leaf_nodes=15,
        min_samples_leaf=50,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=seed,
    )


def _check(X: pd.DataFrame, t: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(t, dtype=int)
    y = np.asarray(y, dtype=int)
    if len(X) != len(t) or len(t) != len(y):
        raise ValueError("X, t and y must have the same length")
    if not set(np.unique(t)) <= {0, 1} or not set(np.unique(y)) <= {0, 1}:
        raise ValueError("t and y must be binary 0/1")
    if t.min() == t.max():
        raise ValueError("need both treated and control customers")
    return t, y


class RandomTargeting:
    """Baseline: contact a random subset (what a blanket campaign achieves per contact)."""

    name = "random"

    def __init__(self, seed: int = 0):
        self.seed = seed

    def fit(self, X, t, y):
        _check(X, t, y)
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return np.random.default_rng(self.seed + 7919).random(len(X))


class ResponseModel:
    """Baseline: classic propensity-to-buy model trained on treated customers.

    It ranks by P(y=1 | x, T=1), i.e. it finds likely buyers, not customers
    whose purchase is *caused* by the contact.
    """

    name = "response_model"

    def __init__(self, seed: int = 0):
        self.model = _clf(seed)

    def fit(self, X, t, y):
        t, y = _check(X, t, y)
        self.model.fit(X[t == 1], y[t == 1])
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(X)[:, 1]


class SLearner:
    """One classifier with the treatment flag as an extra feature."""

    name = "s_learner"

    def __init__(self, seed: int = 0):
        self.model = _clf(seed)

    @staticmethod
    def _with_t(X: pd.DataFrame, value) -> pd.DataFrame:
        Xt = X.copy()
        Xt["__treatment"] = value
        return Xt

    def fit(self, X, t, y):
        t, y = _check(X, t, y)
        self.model.fit(self._with_t(X, t), y)
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        p1 = self.model.predict_proba(self._with_t(X, 1))[:, 1]
        p0 = self.model.predict_proba(self._with_t(X, 0))[:, 1]
        return p1 - p0


class TLearner:
    """Separate outcome classifiers for treated and control; uplift = difference."""

    name = "t_learner"

    def __init__(self, seed: int = 0):
        self.m1 = _clf(seed)
        self.m0 = _clf(seed + 1)

    def fit(self, X, t, y):
        t, y = _check(X, t, y)
        self.m1.fit(X[t == 1], y[t == 1])
        self.m0.fit(X[t == 0], y[t == 0])
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return self.m1.predict_proba(X)[:, 1] - self.m0.predict_proba(X)[:, 1]


def transformed_outcome(y: np.ndarray, t: np.ndarray, propensity: float) -> np.ndarray:
    """Y* = y * (t - e) / (e (1 - e)); E[Y* | x] equals the uplift under randomization."""
    if not 0.0 < propensity < 1.0:
        raise ValueError("propensity must be in (0, 1)")
    return y * (t - propensity) / (propensity * (1.0 - propensity))


class TransformedOutcome:
    """Single regressor fitted directly on the transformed outcome Y*.

    The propensity is the known randomization rate, estimated as the treated
    share of the *training* set.
    """

    name = "transformed_outcome"

    def __init__(self, seed: int = 0):
        # Y* is very noisy (values like -2, 0, +2), so this model is regularized harder.
        self.model = HistGradientBoostingRegressor(
            max_iter=150,
            learning_rate=0.04,
            max_leaf_nodes=7,
            min_samples_leaf=300,
            l2_regularization=5.0,
            early_stopping=False,
            random_state=seed,
        )
        self.propensity_: float | None = None

    def fit(self, X, t, y):
        t, y = _check(X, t, y)
        self.propensity_ = float(t.mean())
        self.model.fit(X, transformed_outcome(y, t, self.propensity_))
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict(X)


SCORERS = {
    cls.name: cls
    for cls in (RandomTargeting, ResponseModel, SLearner, TLearner, TransformedOutcome)
}
UPLIFT_MODELS = ("s_learner", "t_learner", "transformed_outcome")


def make(name: str, seed: int = 0):
    try:
        return SCORERS[name](seed=seed)
    except KeyError:
        raise ValueError(f"unknown model {name!r}; choose from {sorted(SCORERS)}") from None
