"""Feature engineering with a strict day-ahead information cutoff.

The forecast for hour t is issued at t - HORIZON, so every feature derived from
`demand` must come from observations at least HORIZON hours old. Calendar fields,
holiday flags and temperature are treated as known in advance (a weather forecast).
"""
import pandas as pd

HORIZON = 24
LAGS = (24, 48, 168)
ROLL_WINDOWS = (24, 168)

CALENDAR = ["hour", "dayofweek", "is_weekend", "is_holiday", "temperature"]


def build_features(df: pd.DataFrame, horizon: int = HORIZON) -> pd.DataFrame:
    """Return a feature frame indexed like `df` (sorted by timestamp).

    Rows without enough history yield NaN lag features and are dropped by
    `drop_warmup`.
    """
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError("df must be sorted by timestamp")
    out = pd.DataFrame(index=df.index)
    ts = df["timestamp"]
    out["hour"] = ts.dt.hour
    out["dayofweek"] = ts.dt.dayofweek
    out["is_weekend"] = (ts.dt.dayofweek >= 5).astype(int)
    out["is_holiday"] = df["is_holiday"]
    out["temperature"] = df["temperature"]

    demand = df["demand"].astype(float)
    for lag in LAGS:
        out[f"lag_{lag}"] = demand.shift(max(lag, horizon))
    known = demand.shift(horizon)  # newest observation allowed at forecast time
    for w in ROLL_WINDOWS:
        out[f"roll_mean_{w}"] = known.rolling(w).mean()
    return out


def feature_columns(feats: pd.DataFrame) -> list[str]:
    return list(feats.columns)


def drop_warmup(feats: pd.DataFrame, target: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    mask = feats.notna().all(axis=1)
    return feats[mask], target[mask]
