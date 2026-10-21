"""Synthetic food-delivery orders with heteroscedastic, covariate-dependent noise."""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURES = ["distance_km", "items", "hour", "is_rain", "courier_load", "zone"]


def generate_orders(n: int = 12000, days: int = 90, seed: int = 0) -> pd.DataFrame:
    """Generate orders sorted by time. Delivery minutes = prep + travel + noise whose
    scale grows with rain, rush hour and distance (so a constant-width interval must fail)."""
    rng = np.random.default_rng(seed)
    ts = np.sort(rng.uniform(0, days * 24 * 60, n))  # minutes since start
    hour = ((ts // 60) % 24).astype(int)
    # demand concentrated at lunch/dinner
    keep_p = 0.25 + 0.75 * (np.isin(hour, [12, 13, 19, 20, 21]))
    keep = rng.uniform(size=n) < keep_p
    ts, hour = ts[keep], hour[keep]
    m = len(ts)
    distance = rng.gamma(2.2, 1.4, m).clip(0.3, 15)
    items = rng.integers(1, 9, m)
    zone = rng.integers(0, 5, m)
    day = (ts // (24 * 60)).astype(int)
    rain_day = rng.uniform(size=days + 1) < 0.25
    is_rain = (rain_day[day] & (rng.uniform(size=m) < 0.8)).astype(int)
    rush = np.isin(hour, [12, 13, 19, 20]).astype(int)
    courier_load = np.clip(rng.normal(0.5 + 0.3 * rush, 0.15, m), 0, 1)
    zone_prep = np.array([8, 10, 12, 9, 14])[zone]
    mean = zone_prep + 0.9 * items + distance * (3.5 + 1.5 * is_rain) + 8 * courier_load
    scale = 0.06 + 0.10 * is_rain + 0.07 * rush + 0.01 * distance
    minutes = mean * np.exp(rng.normal(0, scale)) + rng.exponential(1.0, m)
    df = pd.DataFrame(
        {"ts_min": ts, "distance_km": distance, "items": items, "hour": hour,
         "is_rain": is_rain, "courier_load": courier_load, "zone": zone, "minutes": minutes}
    )
    return df.reset_index(drop=True)


def time_split(df: pd.DataFrame, train: float = 0.6, calib: float = 0.2):
    """Chronological train / calibration / test split (no shuffling, no overlap)."""
    if train <= 0 or calib <= 0 or train + calib >= 1:
        raise ValueError("train + calib must be < 1")
    d = df.sort_values("ts_min", kind="stable").reset_index(drop=True)
    i, j = int(len(d) * train), int(len(d) * (train + calib))
    return d.iloc[:i].copy(), d.iloc[i:j].copy(), d.iloc[j:].copy()
