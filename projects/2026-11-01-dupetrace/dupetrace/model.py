"""Logistic-regression reranker over pairwise features, persisted as plain JSON.

JSON (not pickle) keeps the artifact inspectable, diff-able and safe to load.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from .features import FEATURES


@dataclass
class Reranker:
    mean: list[float]
    scale: list[float]
    coef: list[float]
    intercept: float
    threshold: float = 0.5  # min top-1 probability to suggest "duplicate"
    features: tuple[str, ...] = tuple(FEATURES)

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, C: float = 1.0, seed: int = 0) -> "Reranker":
        if len(set(y.tolist())) < 2:
            raise ValueError("training data needs both positive and negative pairs")
        scaler = StandardScaler().fit(X)
        lr = LogisticRegression(C=C, max_iter=2000, random_state=seed).fit(scaler.transform(X), y)
        scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
        return cls(mean=scaler.mean_.tolist(), scale=scale.tolist(), coef=lr.coef_[0].tolist(),
                   intercept=float(lr.intercept_[0]))

    def score(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.size == 0:
            return np.zeros(0)
        z = ((X - np.asarray(self.mean)) / np.asarray(self.scale)) @ np.asarray(self.coef) + self.intercept
        return 1.0 / (1.0 + np.exp(-z))

    def weights(self) -> list[tuple[str, float]]:
        """Standardised coefficients, largest magnitude first (for the README / debugging)."""
        return sorted(zip(self.features, self.coef), key=lambda kv: -abs(kv[1]))

    def save(self, path: str | Path) -> None:
        d = {"features": list(self.features), "mean": self.mean, "scale": self.scale, "coef": self.coef,
             "intercept": self.intercept, "threshold": self.threshold}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(d, indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Reranker":
        d = json.loads(Path(path).read_text())
        if d["features"] != FEATURES:
            raise ValueError("model was trained with a different feature set")
        for k in ("mean", "scale", "coef"):
            if len(d[k]) != len(FEATURES) or not all(math.isfinite(v) for v in d[k]):
                raise ValueError(f"corrupt model field: {k}")
        return cls(mean=d["mean"], scale=d["scale"], coef=d["coef"], intercept=d["intercept"],
                   threshold=d["threshold"])
