"""Hyper-parameter selection using only the tail of the training window."""
from itertools import product

import pandas as pd

from ..models.sklearn_models import HGBForecaster
from .metrics import mae

DEFAULT_GRID = {
    "learning_rate": (0.03, 0.1),
    "max_leaf_nodes": (15, 31),
    "max_iter": (200,),
}


def select_hgb_params(X_train: pd.DataFrame, y_train: pd.Series, val_rows: int, seed: int = 0, grid: dict | None = None) -> dict:
    """Pick the grid point with the lowest validation MAE.

    The last `val_rows` rows of the training data are held out for validation, so the
    test window is never touched. Ties go to the earliest grid point (deterministic).
    """
    grid = grid or DEFAULT_GRID
    if not 0 < val_rows < len(X_train):
        raise ValueError("val_rows must be between 1 and len(X_train) - 1")
    X_fit, y_fit = X_train.iloc[:-val_rows], y_train.iloc[:-val_rows]
    X_val, y_val = X_train.iloc[-val_rows:], y_train.iloc[-val_rows:]
    keys = list(grid)
    best, best_score = None, float("inf")
    for values in product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values))
        score = mae(y_val, HGBForecaster(seed=seed, **params).fit(X_fit, y_fit).predict(X_val))
        if score < best_score:
            best, best_score = params, score
    return best
