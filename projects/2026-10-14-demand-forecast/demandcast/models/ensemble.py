import numpy as np
import pandas as pd


class MeanEnsemble:
    name = "ensemble_mean"

    def __init__(self, members: list, fit_members: bool = True):
        if not members:
            raise ValueError("ensemble needs at least one member")
        self.members = members
        self.fit_members = fit_members

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "MeanEnsemble":
        if self.fit_members:
            for m in self.members:
                m.fit(X, y)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.mean([m.predict(X) for m in self.members], axis=0)
