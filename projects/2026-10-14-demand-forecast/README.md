# demandcast — leakage-safe day-ahead hourly demand forecasting

## Problem
Forecast hourly demand (bike-share / ride-hail style) 24 hours ahead. Time-series ML projects often leak
future information through lag/rolling features or shuffled splits; this project makes the information
cutoff explicit and tests it.

## Architecture
```
demandcast/data.py      synthetic generator (daily/weekly seasonality, temperature, holidays, trend, Poisson noise)
demandcast/features.py  calendar + known-ahead covariates + demand lags/rolling means, all >= 24h old
demandcast/evaluate.py  chronological split, MAE/RMSE/WAPE, models, experiment runner, JSON report
demandcast/__main__.py  CLI (`run`, `generate`)
tests/                  pytest suite (leakage, split order, metrics, reproducibility)
samples/                sample input CSV + expected metrics
```

## Setup
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage
```
python -m demandcast run --days 365 --seed 42 --out outputs   # writes outputs/metrics.json
python -m demandcast generate --days 3 --out data.csv
python -m pytest -q
```

## Results (measured, `--days 365 --seed 42`, test = last 20% of the timeline)
| Model | MAE | RMSE | WAPE |
|---|---|---|---|
| Seasonal naive (24h lag) | 11.115 | 16.775 | 0.3246 |
| Seasonal naive (168h lag) — strongest baseline | 6.423 | 8.801 | 0.1876 |
| Ridge (one-hot hour/dow + numeric) | 6.159 | 8.088 | 0.1798 |
| HistGradientBoosting | 5.245 | 7.187 | 0.1532 |

Full output: `samples/expected_metrics.json` (reruns are bit-identical on the pinned-range library versions tested:
Python 3.13, numpy 2.5, pandas 3.0, scikit-learn 1.9). Results are on synthetic data and say nothing about real demand.

## Design decisions
- **Primary metric: MAE** (interpretable in units of demand); RMSE and WAPE reported too.
- **Chronological split**, no shuffling; train ends strictly before test starts.
- **Leakage prevention:** demand features use `shift(>=24)`; rolling means are computed on the already-shifted series.
  A test mutates demand inside the forbidden window and asserts earlier features are unchanged.
- Features are built on the whole series before splitting; that is safe only because they look strictly backward
  (verified by the test), and no scaler/encoder is fit outside the training set (Ridge pipeline is fit on train only).
- Baselines first: seasonal naive at 24h and 168h; the model must beat both (asserted in a test on a smaller run).
- Temperature is treated as a known-ahead covariate (an idealized perfect weather forecast).

## Limitations
- Synthetic data only; the generator's structure favours models that capture hour×weekday interactions.
- Single train/test split (no rolling-origin CV), no hyperparameter tuning, no prediction intervals.
- Perfect-forecast temperature is optimistic; real deployments would use forecast error.
- The warm-up period (first 191 hours) is dropped.
