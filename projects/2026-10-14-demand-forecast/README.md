# demandcast — leakage-safe day-ahead hourly demand forecasting

## Problem
Forecast hourly demand (bike-share / ride-hail style) 24 hours ahead. Time-series ML projects often leak
future information through lag/rolling features, shuffled splits, or tuning on the test set. This project
makes the information cutoff explicit, tests it, and evaluates with a rolling-origin backtest.

## Architecture
```
demandcast/
  config.py              ExperimentConfig dataclass: validation, JSON load/save
  data/generator.py      synthetic data: seasonality, holidays, trend, Poisson noise, noisy temperature *forecast*
  features/builder.py    FeatureSpec + FeatureBuilder: calendar, forecast covariates, lags, rolling stats (>= horizon old)
  models/                Forecaster protocol; SeasonalNaive, Ridge, HistGradientBoosting, MeanEnsemble,
                         QuantileInterval (10%/90%), registry
  evaluation/            splits (rolling-origin folds), metrics (MAE/RMSE/WAPE/pinball/coverage),
                         tuning (grid search on tail of train only), backtest (orchestration)
  reporting.py           metrics.json + report.md
  serving.py             train final model, save/load (joblib), score new CSV rows
  cli.py                 run | train | predict | generate
tests/                   24 tests: leakage at several horizons, splits, metrics, tuning, backtest, serving, CLI
samples/                 config.json, sample_input.csv, expected/{metrics.json,report.md}
```
Data flow: `generate_demand → FeatureBuilder.transform_xy → rolling_origin_splits → per fold: tune → fit all models + interval → score → aggregate`.

## Setup
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage
```
python -m demandcast run --config samples/config.json --out outputs      # backtest (~25 s) -> metrics.json, report.md
python -m demandcast generate --days 120 --out data.csv
python -m demandcast train --data data.csv --model-out model.joblib
python -m demandcast predict --model model.joblib --input data.csv --out preds.csv
python -m pytest -q                                                       # ~45 s
```
`predict` input needs columns `timestamp, temp_forecast, is_holiday, demand` (hourly, gap-free). Each scored row uses only
demand at least `horizon` hours older; the first 504 rows (warm-up) get no prediction. Load only model files you created
(joblib uses pickle).

## Results (measured: `samples/config.json`, 4 folds × 21-day test windows, mean ± std over folds)
| Model | MAE | WAPE |
|---|---|---|
| seasonal naive 24h | 11.338 ± 1.210 | 0.319 ± 0.011 |
| seasonal naive 168h (best baseline) | 6.500 ± 0.504 | 0.183 ± 0.010 |
| ridge | 5.684 ± 0.504 | 0.160 ± 0.008 |
| hist gradient boosting (tuned per fold) | 5.774 ± 0.628 | 0.162 ± 0.006 |
| ensemble (ridge + HGB mean) | **5.586 ± 0.585** | 0.157 ± 0.006 |

80% prediction interval: empirical coverage **0.689 ± 0.025** (under the nominal 0.80), mean width 15.2.
Full tables, including segment WAPE and per-fold hyper-parameters: `samples/expected/`. Reruns are identical here
(Python 3.13, numpy 2.5, pandas 3.0, scikit-learn 1.9).

## Design decisions
- **Rolling-origin (expanding window) CV** instead of one split: gives a spread across periods, not a single lucky number.
- **Leakage controls:** demand features use `shift(>= horizon)`, rolling stats are computed on the shifted series, tests mutate
  demand inside the forbidden window for horizons 1/24/48; hyper-parameters are chosen on the last 28 days *of the training
  window*; the backtest asserts each fold's training data ends before its test window.
- **Noisy temperature forecast:** demand depends on true temperature, models see only a forecast with Gaussian error.
- **Baselines first**, and a deterministic grid-search tie-break so results reproduce.
- **Quantile intervals** from separate quantile regressors, post-fixed to be ordered and non-negative.
- Models share a small `Forecaster` protocol, so adding one means adding a factory in `models/registry.py`.

## Limitations
- Synthetic data. The generator is mostly additive in hour/weekday structure, which is why a linear model with one-hot
  hour/weekday is as good as boosting here (the tuned HGB is not better than Ridge; the ensemble is best by a small margin
  within fold-to-fold std). Conclusions may not transfer to real demand.
- Intervals under-cover (68.9% vs 80%) and are uncalibrated; no conformal recalibration yet.
- Only a small grid is tuned; the ensemble is an unweighted mean.
- Gap-free hourly input is required; no missing-data handling. No multi-step recursive forecasts beyond the fixed horizon.
