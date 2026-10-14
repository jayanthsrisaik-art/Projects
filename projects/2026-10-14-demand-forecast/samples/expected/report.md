# Backtest report

4 rolling-origin folds, 21-day test windows, 24h horizon, seed 42.

## Point forecasts (mean ± std over folds)

| Model | MAE | RMSE | WAPE |
|---|---|---|---|
| seasonal_naive_24h | 11.338 ± 1.210 | 17.163 ± 2.093 | 0.319 ± 0.011 |
| seasonal_naive_168h | 6.500 ± 0.504 | 8.868 ± 0.734 | 0.183 ± 0.010 |
| ridge | 5.684 ± 0.504 | 7.669 ± 0.712 | 0.160 ± 0.008 |
| hist_gradient_boosting | 5.774 ± 0.628 | 7.800 ± 0.770 | 0.162 ± 0.006 |
| ensemble_ridge_hgb | 5.586 ± 0.585 | 7.554 ± 0.744 | 0.157 ± 0.006 |

Best by MAE: **ensemble_ridge_hgb**

## Prediction interval
Nominal coverage 80%.

| Metric | mean ± std |
|---|---|
| coverage | 0.689 ± 0.025 |
| mean_width | 15.182 ± 0.541 |
| pinball_lo | 1.396 ± 0.227 |
| pinball_hi | 1.272 ± 0.096 |

## WAPE by segment (pooled test rows)

| Segment | Rows | seasonal_naive_24h | seasonal_naive_168h | ridge | hist_gradient_boosting | ensemble_ridge_hgb |
|---|---|---|---|---|---|---|
| weekday | 1440 | 0.2605 | 0.1807 | 0.1580 | 0.1600 | 0.1556 |
| weekend_or_holiday | 576 | 0.5132 | 0.1863 | 0.1629 | 0.1673 | 0.1591 |
