# logpulse

Log ingestion → template mining → per-minute feature aggregation in SQL → incident-window anomaly detection (IsolationForest vs. a z-score baseline), exposed through a small FastAPI service. Combines data engineering (SQLite/SQL features), software engineering (parser, API, tests) and ML (chronological, leakage-safe evaluation).

## Problem
On-call engineers get thousands of log lines per minute. Which *minutes* look like an incident — an error burst of a new failure mode, a latency spike, or traffic silently dropping — without hand-written rules per service?

## Architecture
```
generator.py  synthetic logs + labelled incidents (seeded)
parsing.py    regex parser -> LogRecord (rejects malformed lines)
templates.py  mask digit-bearing tokens -> template id (sha1)
store.py      SQLite: logs, templates(first_seen); SQL GROUP BY minute features
detect.py     feature matrix, z-score baseline, IsolationForest, metrics
api.py        FastAPI: POST /ingest, GET /templates, GET /anomalies
cli.py        generate / eval
```
Features per minute: log volume, error rate, fraction of lines with a *new* template, distinct templates, mean/max latency, and volume ratio vs. the median of the previous 30 minutes.

## Setup & usage
```
pip install -r requirements.txt
python -m pytest -q
python -m logpulse.cli generate --out /tmp/logs.log      # synthetic stream
python -m logpulse.cli eval [--quantile 0.95]            # train/test report as JSON
uvicorn --factory logpulse.api:create_app                # LOGPULSE_DB=path.db to persist
```
Sample input: `sample/logs_sample.txt` with expected parse output `sample/expected_parsed.json`; recorded eval output in `sample/expected_eval.json`.

## Evaluation (measured, seed 7, 3 days, 36,991 lines, 14 incidents)
Chronological split: first 60% of minutes = train, remaining 40% (1,713 minutes, 6 incidents, 65 incident minutes) = test. Scaler, forest, alert threshold (a quantile of *training* scores) and the "new template" vocabulary (templates first seen before the split) are derived from the training period only. `count_ratio` uses only past windows.

| Detector | Threshold | Precision | Recall | F1 | Avg. precision | Incidents hit |
|---|---|---|---|---|---|---|
| z-score on error count (baseline) | z>3 | 1.00 | 0.31 | 0.47 | 0.34 | 2/6 |
| IsolationForest | train q=0.99 (default) | 0.55 | 0.09 | 0.16 | 0.61 | 3/6 |
| IsolationForest | train q=0.95 | 0.57 | 0.86 | 0.68 | 0.61 | 6/6 |
| IsolationForest | train q=0.90 | 0.30 | 0.91 | 0.45 | 0.61 | 6/6 |

Average precision (threshold-free) is the fairest comparison: 0.61 vs 0.34. The threshold rows are a sensitivity analysis run after seeing test results; q=0.95 is *not* an untouched held-out number, and the default (0.99) performs poorly because training data itself contains incidents, which inflates the 99th percentile.

## Design decisions
- Novelty is computed in SQL from `templates.first_seen` against the split time, so test-period vocabulary can't leak into training features.
- Hard negatives: after 70% of the timeline a harmless new "cache warmup" template appears; novelty alone must not be enough.
- Unsupervised model; labels are used only for evaluation.

## Limitations
- Synthetic data with three simple incident types; real logs are messier and results will not transfer directly.
- Template mining is a digit-masking heuristic (no Drain-style tree); templates with variable words are not merged.
- Single seed; no confidence intervals. Threshold selection is a manual hyperparameter.
- The API refits per request, holds one SQLite connection, and has no auth.
- Tested on Python 3.13 / scikit-learn 1.9 / numpy 2.5; the IsolationForest scores may vary slightly across versions.
