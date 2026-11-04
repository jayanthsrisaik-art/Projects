"""Model factory and out-of-fold probability estimation."""
from __future__ import annotations

import numpy as np
from sklearn.base import clone
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MODELS = ("logreg", "rf")


def make_model(name: str = "logreg", seed: int = 0):
    if name == "logreg":
        return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=2000))
    if name == "rf":
        return RandomForestClassifier(n_estimators=200, min_samples_leaf=1, n_jobs=1, random_state=seed)
    raise ValueError(f"unknown model {name!r}; choose from {MODELS}")


def _full_proba(model, x: np.ndarray, k: int) -> np.ndarray:
    """predict_proba padded to all K classes (a fold may miss a rare class)."""
    p = model.predict_proba(x)
    out = np.zeros((x.shape[0], k))
    out[:, model.classes_.astype(int)] = p
    return out


def out_of_fold_proba(x: np.ndarray, y: np.ndarray, model_name: str = "logreg",
                      folds: int = 5, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Every row is scored by a model that never saw it. Returns (pred_probs, fold_id)."""
    y = np.asarray(y, dtype=int)
    k = int(y.max()) + 1
    min_count = np.bincount(y, minlength=k)
    min_count = min_count[min_count > 0].min()
    folds = max(2, min(folds, int(min_count)))
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    probs = np.full((len(y), k), np.nan)
    fold_id = np.full(len(y), -1)
    base = make_model(model_name, seed)
    for f, (tr, va) in enumerate(skf.split(x, y)):
        m = clone(base).fit(x[tr], y[tr])
        probs[va] = _full_proba(m, x[va], k)
        fold_id[va] = f
    assert not np.isnan(probs).any() and (fold_id >= 0).all()
    return probs, fold_id


def in_sample_proba(x: np.ndarray, y: np.ndarray, model_name: str = "logreg", seed: int = 0) -> np.ndarray:
    """LEAKY on purpose: fit and score the same rows. Used only to measure the damage."""
    y = np.asarray(y, dtype=int)
    m = make_model(model_name, seed).fit(x, y)
    return _full_proba(m, x, int(y.max()) + 1)
