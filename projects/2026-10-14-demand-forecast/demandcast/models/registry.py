"""Factories for the models compared in the backtest."""
from .baselines import SeasonalNaive
from .ensemble import MeanEnsemble
from .sklearn_models import HGBForecaster, RidgeForecaster


def build_models(seed: int = 0, hgb_params: dict | None = None) -> dict:
    """Return name -> *unfitted* model. The ensemble owns its members and fits them."""
    hgb_params = hgb_params or {}
    return {
        "seasonal_naive_24h": SeasonalNaive("lag_24"),
        "seasonal_naive_168h": SeasonalNaive("lag_168"),
        "ridge": RidgeForecaster(),
        "hist_gradient_boosting": HGBForecaster(seed=seed, **hgb_params),
        "ensemble_ridge_hgb": MeanEnsemble([RidgeForecaster(), HGBForecaster(seed=seed, **hgb_params)]),
    }
