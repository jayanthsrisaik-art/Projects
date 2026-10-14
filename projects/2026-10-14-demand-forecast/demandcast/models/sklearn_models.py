import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

DEFAULT_HGB_PARAMS = {"learning_rate": 0.05, "max_leaf_nodes": 31, "max_iter": 300}


class RidgeForecaster:
    name = "ridge"

    def __init__(self, alpha: float = 1.0):
        pre = ColumnTransformer(
            [("cat", OneHotEncoder(handle_unknown="ignore"), ["hour", "dayofweek"])],
            remainder=StandardScaler(),
        )
        self.pipe = make_pipeline(pre, Ridge(alpha=alpha))

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "RidgeForecaster":
        self.pipe.fit(X, y)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.clip(self.pipe.predict(X), 0, None)


class HGBForecaster:
    name = "hist_gradient_boosting"

    def __init__(self, seed: int = 0, **params):
        self.params = {**DEFAULT_HGB_PARAMS, **params}
        self.model = HistGradientBoostingRegressor(random_state=seed, **self.params)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "HGBForecaster":
        self.model.fit(X, y)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.clip(self.model.predict(X), 0, None)
