"""Synthetic hourly demand generator (bike-share / ride-hail style)."""
import numpy as np
import pandas as pd

HOLIDAY_RATE = 0.02


def generate_demand(
    days: int = 365,
    seed: int = 42,
    start: str = "2024-01-01",
    temp_noise_std: float = 1.5,
) -> pd.DataFrame:
    """Return hourly data: timestamp, temperature, temp_forecast, is_holiday, demand.

    Demand = Poisson(base * daily profile * temperature factor * holiday factor * trend),
    driven by the *true* temperature. `temp_forecast` is the true value plus Gaussian
    error and is the only temperature a model may use. Fully determined by `seed`.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=days * 24, freq="h")
    hour = ts.hour.to_numpy()
    dow = ts.dayofweek.to_numpy()
    doy = ts.dayofyear.to_numpy()

    holiday_days = rng.random(days) < HOLIDAY_RATE
    is_holiday = np.repeat(holiday_days, 24).astype(int)
    weekend = (dow >= 5) | (is_holiday == 1)

    commute = np.exp(-0.5 * ((hour - 8) / 1.5) ** 2) + np.exp(-0.5 * ((hour - 17.5) / 2.0) ** 2)
    leisure = np.exp(-0.5 * ((hour - 14) / 3.5) ** 2)
    profile = np.where(weekend, 0.9 * leisure + 0.1, 0.25 + 1.0 * commute)

    temp_day = 12 + 10 * np.sin(2 * np.pi * (doy - 110) / 365.25)
    walk = np.cumsum(rng.normal(0, 0.15, ts.size))
    walk -= pd.Series(walk).rolling(24 * 7, center=True, min_periods=1).mean().to_numpy()
    temperature = temp_day + 4 * np.sin(2 * np.pi * (hour - 9) / 24) + walk
    temp_factor = np.clip(1 + 0.04 * (temperature - 10), 0.3, 1.8)

    trend = 1 + 0.15 * np.arange(ts.size) / ts.size
    rate = 60 * profile * temp_factor * np.where(is_holiday == 1, 0.8, 1.0) * trend
    demand = rng.poisson(rate)
    temp_forecast = temperature + rng.normal(0, temp_noise_std, ts.size)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "temperature": temperature.round(2),
            "temp_forecast": temp_forecast.round(2),
            "is_holiday": is_holiday,
            "demand": demand.astype(int),
        }
    )
