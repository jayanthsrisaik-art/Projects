# persuade – uplift modelling for budget-limited promo targeting

## Problem
A retailer can send a discount email to only 20% of its customers. The usual approach is a
"propensity to buy" model: contact whoever is most likely to purchase. That wastes budget on
**sure things** (they would buy anyway) and can actively hurt sales by contacting **sleeping dogs**
(loyal full-price buyers who react badly to a discount push). What matters is the *causal* effect of the
email on each customer, `P(buy | email) − P(buy | no email)` – the uplift.

persuade simulates a randomized campaign with a known per-customer effect, trains two baselines and three
uplift learners on the training split, and measures on a held-out split how much incremental revenue
each targeting policy would really have produced.

## Architecture
```
persuade/data.py      synthetic randomized campaign (features, treatment, outcome, oracle effects),
                      stratified train/test split, leakage-safe feature matrix
persuade/models.py    RandomTargeting, ResponseModel (baselines); SLearner, TLearner,
                      TransformedOutcome (uplift learners) – all HistGradientBoosting
persuade/metrics.py   Qini curve / Qini area, observed uplift in top-k (observable on real data);
                      oracle policy value, sleeping-dog share, Spearman vs true uplift (simulation only)
persuade/pipeline.py  generate -> split -> fit(train) -> score(test) -> evaluate, repeated over seeds;
                      score_customers() for ranking a new customer file
persuade/cli.py       python -m persuade evaluate | score | sample
```

| scorer | what it ranks by |
|---|---|
| `random` | baseline: random order (a blanket campaign's per-contact yield) |
| `response_model` | baseline: `P(buy | x, emailed)` from a classifier fit on treated customers only |
| `s_learner` | one classifier with the treatment flag as a feature; `f(x,1) − f(x,0)` |
| `t_learner` | separate treated / control classifiers; `p1(x) − p0(x)` |
| `transformed_outcome` | regressor on `Y* = y(t−e)/(e(1−e))`, whose conditional mean is the uplift under randomization |

### Simulated data
30,000 customers per seed: tenure, days since last order, orders in 90 days, average basket, share of
past orders bought on discount, prior email open rate, app user flag, region. Treatment is a fair coin.
Baseline purchase probability rises with frequency/recency; the effect (on the logit scale) rises with
discount affinity, email engagement and being lapsed, is lower for customers who just bought (sure things)
and is strongly negative for heavy long-tenured buyers (sleeping dogs). On the test split ≈39% of customers
have a negative true effect, and the average effect is about +2.3 pp.

## Setup / usage
```
pip install -r requirements.txt      # tested with Python 3.13, scikit-learn 1.9.1, pandas 3.0.5, numpy 2.5.3
python -m persuade evaluate --out samples/expected_report.json      # 5 seeds, ~20 s
python -m persuade score --input samples/customers_sample.csv --output scores.csv
python -m pytest -q
```
- `evaluate` options: `--n`, `--seeds 0 1 2`, `--budget 0.2`.
- `score` options: `--model {s_learner,t_learner,transformed_outcome}`, `--budget`, `--train-n`, `--seed`.
  It trains on a simulated campaign and writes `customer_id, predicted_uplift, treat, rank`. `treat`
  marks the top `budget` share but never a customer whose predicted uplift is ≤ 0.
- `sample --output file.csv` writes a feature-only customer file from an unseen seed.

Samples: `samples/customers_sample.csv` (input schema, 20 customers),
`samples/expected_scores.csv` (output of the `score` command above),
`samples/expected_report.json` (full measured output of the default `evaluate` run, per seed and summary).

## Measured results
Default run: 5 seeds (0–4), 30,000 customers each, stratified 70/30 split → 9,000 test customers,
budget = top 20% (1,800 contacts). Mean ± std over the seeds, all on the test split.

| model | Qini area | observed uplift in top 20% | expected incremental buyers* | capture of best* | contacted with negative effect* | Spearman vs true uplift* |
|---|---|---|---|---|---|---|
| random | −0.0001 ± 0.0010 | 0.027 ± 0.012 | 43.1 ± 7.2 | 0.089 | 39.2% | −0.005 |
| response_model | 0.0164 ± 0.0007 | 0.221 ± 0.018 | 381.1 ± 7.9 | 0.789 | 10.9% | 0.648 |
| s_learner | 0.0230 ± 0.0008 | 0.250 ± 0.018 | 451.4 ± 6.7 | 0.935 | 0.4% | 0.937 |
| t_learner | 0.0227 ± 0.0012 | 0.251 ± 0.022 | 444.8 ± 6.1 | 0.921 | 0.6% | 0.909 |
| transformed_outcome | 0.0231 ± 0.0009 | 0.245 ± 0.018 | 447.4 ± 5.4 | 0.927 | 0.5% | 0.935 |

\* oracle metrics, computable only because the simulation knows each customer's true effect. The best
possible 1,800-customer selection yields 482.7 expected incremental buyers.

Reading the table:
- All three uplift learners beat the response model by ~65–70 incremental buyers per 1,800 contacts
  (≈ +17%), and stop contacting sleeping dogs (0.4–0.6% of contacts vs 10.9%).
- The response model is far better than random here, because in this simulation many high-propensity
  customers are also persuadable. Its loss comes from the sure things and sleeping dogs it ranks highly.
- The three uplift learners are within about one standard deviation of each other on every metric;
  this experiment does not show a clear winner among them.
- The observable metrics (Qini area, observed top-20% uplift) rank the models the same way as the
  oracle metrics, which is what makes them usable on real data where true effects are unknown.

## Design decisions
- **Randomized data is required.** Uplift is only identifiable because treatment is independent of
  features; a test checks feature balance across arms and that the observed ATE matches the true ATE.
- **Leakage prevention.** `feature_matrix()` emits a fixed whitelist of pre-treatment columns.
  `coupon_redeemed` (recorded after the campaign, only possible for treated buyers) and the `oracle_*`
  columns are never model inputs; tests assert both the exclusion and why the column would leak.
- **Reproducible split.** `train_test_split` with a fixed seed, stratified on the four
  treatment × outcome cells. The transformed-outcome propensity is estimated from the training split only.
- **Two kinds of metrics.** Qini area and observed top-k uplift need only `(y, t)` and work on real
  campaigns; oracle metrics show how well those proxies track the real objective.
- **Multiple seeds.** Each seed regenerates the data, re-splits and refits, so the ± reflects
  data and split variance, not only model randomness.
- **Same learner family everywhere** (HistGradientBoosting with fixed hyper-parameters) so differences
  come from the uplift formulation, not from model capacity.

## Limitations
- Synthetic data with a hand-designed effect surface; real uplift signals are much weaker and noisier,
  and the ordering between learners may change.
- Assumes a single randomized experiment with no interference between customers and a fixed
  treatment rate; observational data would need propensity modelling and overlap checks.
- No hyper-parameter tuning and no confidence intervals beyond the 5-seed spread; the ± is not a
  significance test.
- Budget allocation is a simple top-k cut; it ignores per-customer contact cost and margin.
- `score` trains on simulated data, so its scores are only meaningful for customers drawn from the same
  simulated population. Plugging in real data would mean replacing `data.generate` with a real experiment log.
