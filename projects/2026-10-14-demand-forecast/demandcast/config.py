"""Validated experiment configuration (loadable from JSON)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class ExperimentConfig:
    days: int = 365
    seed: int = 42
    horizon: int = 24             # forecast issued this many hours before the target hour
    n_folds: int = 4              # rolling-origin backtest folds
    test_days: int = 21           # length of each fold's test window
    val_days: int = 28            # tail of each training set used for tuning
    gap_hours: int = 0            # optional embargo between train end and test start
    temp_noise_std: float = 1.5   # error of the temperature forecast (deg C)
    quantiles: tuple[float, float] = (0.1, 0.9)
    tune: bool = True

    def __post_init__(self):
        if self.horizon < 1:
            raise ValueError("horizon must be >= 1")
        if self.n_folds < 1 or self.test_days < 1 or self.val_days < 1:
            raise ValueError("n_folds, test_days and val_days must be >= 1")
        if self.gap_hours < 0 or self.temp_noise_std < 0:
            raise ValueError("gap_hours and temp_noise_std must be >= 0")
        lo, hi = self.quantiles
        if not 0 < lo < hi < 1:
            raise ValueError("quantiles must satisfy 0 < lo < hi < 1")
        # Rough capacity check; the backtest re-checks against the exact warm-up length.
        needed = (self.n_folds * self.test_days + 2 * self.val_days) * 24 + self.gap_hours
        if self.days * 24 <= needed:
            raise ValueError(f"days={self.days} too small for this fold layout (need > {needed // 24} days)")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["quantiles"] = list(self.quantiles)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentConfig":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        d = dict(d)
        if "quantiles" in d:
            d["quantiles"] = tuple(d["quantiles"])
        return cls(**d)

    @classmethod
    def from_json(cls, path: str | Path) -> "ExperimentConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))
