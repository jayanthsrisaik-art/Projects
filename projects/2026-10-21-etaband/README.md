# etaband – calibrated delivery-time prediction intervals

## Problem
"Your order arrives in 32 min" is a point guess. Ops and customers need a *range* that is right ~90% of
the time, including on bad days (rain, rush hour) where noise is larger. A constant ± margin is
over-wide on easy orders and too narrow on hard ones. etaband compares three interval methods on synthetic
food-delivery data with a chronological evaluation.

## Architecture
```
etaband/data.py       synthetic orders (heteroscedastic noise) + chronological train/calib/test split
etaband/conformal.py  SplitConformalRidge (baseline), QuantileGBM (raw / CQR)
etaband/metrics.py    coverage, width, Winkler interval score, MAE, per-segment coverage
etaband/pipeline.py   generate -> split -> fit(train) -> calibrate(calib) -> evaluate(test)
etaband/cli.py        `python -m etaband`
```
Methods (target coverage 1-α = 0.90):
1. **ridge_split_conformal** – baseline: Ridge point forecast ± the conformal quantile of |residual|.
2. **quantile_gbm_raw** – gradient-boosted 5% / 95% quantile regressors, uncalibrated.
3. **cqr_gbm** – the same quantile models, widened by the conformalized-quantile-regression correction
   computed on a held-out calibration block.

## Setup / usage
```
pip install -r requirements.txt     # tested with Python 3.13, scikit-learn 1.9, pandas 3.0
python -m etaband --out samples/expected_report.json
python -m pytest -q
```
Options: `--n` (orders generated before peak-hour thinning), `--seed`, `--alpha`.
`samples/orders_sample.csv` shows the data schema; `samples/expected_report.json` is the measured output of the default run.

## Measured results (default run, seed 0, test = last 20% of time, 954 orders)
| model | coverage | mean width (min) | interval score | median-pred MAE | worst-segment coverage |
|---|---|---|---|---|---|
| ridge_split_conformal | 0.896 | 18.25 | 30.83 | 4.44 | 0.773 |
| quantile_gbm_raw | 0.895 | 16.57 | 22.35 | 4.09 | 0.868 |
| cqr_gbm | 0.897 | 16.79 | 22.34 | 4.09 | 0.868 |

Marginal coverage is on target for all three, but the constant-width baseline under-covers its worst
segment (0.77) and is wider; the GBM intervals adapt to context. On this particular split the raw
quantile model was already close to calibrated, so CQR's correction is tiny (+0.2 min width); its
value is the finite-sample guarantee when the raw model is *not* calibrated. These numbers come from one
dataset seed; the test suite checks the qualitative ordering on another seed, not statistical significance.

## Design decisions
- **Chronological split** with train < calibration < test, asserted in code, so there is no leakage from future orders; the calibration block is never used to fit.
- **Finite-sample conformal rank** `ceil((n+1)(1-α))` rather than a plain empirical quantile.
- Quantile crossing is repaired by sorting lo/hi before calibration shift.
- Interval score and worst-segment coverage are reported because marginal coverage alone hides the baseline's failure.

## Limitations
- Data is synthetic; real ETA data has censoring, couriers, and non-stationary drift.
- Conformal guarantees assume exchangeability between calibration and test; a time split with drift only approximately satisfies it (no adaptive/online recalibration here).
- Guarantee is marginal, not conditional per segment (the segment table shows the gap).
- Single seed in the report; no hyper-parameter tuning.
