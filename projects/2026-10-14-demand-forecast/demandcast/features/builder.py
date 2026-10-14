"""Feature engineering with a strict information cutoff.

A forecast for hour t is issued at t - horizon, so every feature derived from `demand`
comes from observations at least `horizon` hours old. Calendar fields, the holiday flag
and the temperature *forecast* are treated as known in advance.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

REQUIRED_COLUMNS = ("timestamp", "temp_forecast", "is_holiday", "demand")


@dataclass(frozen=True)
class FeatureSpec:
    horizon: int = 24
    lags: tuple[int, ...] = (24, 48, 168)
    roll_windows: tuple[int, ...] = (24, 168)
    weeks_back: int = 3  # mean of the same hour over the last N weeks


class FeatureBuilder:
    def __init__(self, spec: FeatureSpec | None = None):
        self.spec = spec or FeatureSpec()

    @property
    def warmup(self) -> int:
        """Leading rows whose features contain NaN and must be dropped."""
        s = self.spec
        lag_need = max(max(s.lags, default=0), s.horizon)
        roll_need = s.horizon + max(s.roll_windows, default=1) - 1
        week_need = max(168 * s.weeks_back, s.horizon)
        return max(lag_need, roll_need, week_need)

    def _check(self, df: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"missing columns: {missing}")
        steps = df["timestamp"].diff().dropna()
        if (steps <= pd.Timedelta(0)).any():
            raise ValueError("df must be sorted by timestamp without duplicates")
        if (steps != pd.Timedelta(hours=1)).any():
            raise ValueError("timestamps must be a gap-free hourly series")

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """Feature frame indexed like `df`; the first `warmup` rows contain NaN."""
        self._check(df)
        s = self.spec
        ts = df["timestamp"]
        out = pd.DataFrame(index=df.index)
        out["hour"] = ts.dt.hour
        out["dayofweek"] = ts.dt.dayofweek
        out["is_weekend"] = (ts.dt.dayofweek >= 5).astype(int)
        out["is_holiday"] = df["is_holiday"]
        out["temp_forecast"] = df["temp_forecast"]

        demand = df["demand"].astype(float)
        for lag in s.lags:
            out[f"lag_{lag}"] = demand.shift(max(lag, s.horizon))
        known = demand.shift(s.horizon)  # newest observation available at forecast time
        for w in s.roll_windows:
            out[f"roll_mean_{w}"] = known.rolling(w).mean()
        out["roll_std_24"] = known.rolling(24).std()
        same_hour = [demand.shift(max(168 * k, s.horizon)) for k in range(1, s.weeks_back + 1)]
        out[f"same_hour_mean_{s.weeks_back}w"] = pd.concat(same_hour, axis=1).mean(axis=1, skipna=False)
        return out

    def transform_xy(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
        """Features and target with warm-up rows removed (index preserved)."""
        feats = self.transform(df)
        mask = feats.notna().all(axis=1)
        return feats[mask], df.loc[mask, "demand"]
