import numpy as np
import pandas as pd


class SeasonalNaive:
    """Predict with an existing lag feature (no learning)."""

    def __init__(self, column: str):
        self.column = column
        self.name = f"seasonal_naive[{column}]"

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "SeasonalNaive":
        if self.column not in X.columns:
            raise ValueError(f"feature {self.column!r} not available")
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return X[self.column].to_numpy(dtype=float)
