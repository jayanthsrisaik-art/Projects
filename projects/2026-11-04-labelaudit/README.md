# labelaudit – finding mislabeled training rows with confident learning

## Problem
Training labels are often wrong: annotators confuse similar classes, heuristics mislabel edge cases,
and stale rules stay in place. A model trained on those labels learns the mistakes, and an evaluation
set labelled the same way hides them. Relabelling everything costs too much. You need a short,
ranked list of rows worth a second look, plus an estimate of *how much* noise exists and *which*
classes get confused.

labelaudit implements **confident learning** (Northcutt, Jiang & Chuang, JAIR 2021) from scratch in
NumPy. It runs on any numeric-feature CSV, and it includes a benchmark that injects known label noise
into scikit-learn's bundled handwritten-digits data, so every flagged row can be checked against
ground truth.

## Architecture
```
labelaudit/data.py       digits / synthetic data, per-class noise injection (asymmetric digit
                         confusions such as 1↔7 and 3↔8, or uniform), stratified split, CSV I/O
labelaudit/models.py     model factory (random forest, logistic regression), K-fold out-of-fold probabilities
labelaudit/confident.py  per-class thresholds -> confident joint -> calibrated joint Q(given,true)
                         -> noise matrix; issue finders: confident_joint / prune_by_noise_rate / both
labelaudit/evaluate.py   baselines, precision/recall/F1 vs known flips, noise-matrix error,
                         retrain-after-pruning accuracy on a CLEAN held-out test set
labelaudit/cli.py        make-sample | audit | score | bench
```
How the algorithm works:
1. **Out-of-fold probabilities** `p̂(k | x)`: every row is scored by a model trained on the other folds.
2. **Per-class threshold** `t_j`: the mean `p̂(j)` over rows *labelled* `j`, which gives each class its own confidence bar.
3. **Confident joint** `C[i][j]`: counts rows labelled `i` whose `p̂(j) ≥ t_j`. If several classes pass, the row goes to the one with the highest probability. Rows that pass no threshold are not counted.
4. **Calibration**: each row of `C` is rescaled to the real count of label `i`, then normalised to a joint distribution `Q`. From `Q` it reports the estimated noise rate `1 − trace(Q)` and the noise matrix `P(given | true)`.
5. **prune_by_noise_rate** (the default): for each off-diagonal cell, it flags `round(n·Q[i][j])` rows labelled `i` with the largest `p̂(j) − p̂(i)`. Flagged rows are ranked by normalised margin, most suspicious first, and each one gets a suggested label.

## Setup / usage
```
pip install -r requirements.txt       # tested: Python 3.11, numpy 2.4, scikit-learn 1.9
python -m pytest -q                   # 20 tests, ~12 s

python -m labelaudit make-sample --out samples            # noisy digits train set + truth.csv
python -m labelaudit audit --csv samples/digits_noisy.csv --label label \
       --out issues.csv --report report.json              # ranked issues + noise summary
python -m labelaudit score --issues issues.csv --truth samples/truth.csv
python -m labelaudit bench --out bench.json               # full benchmark (~2–3 min)
```
`audit` works on any CSV with numeric feature columns, a label column (any strings), and an optional
`id` column. Options: `--method {prune_by_noise_rate,confident_joint,both}`, `--model {rf,logreg}`,
`--folds`, `--seed`.

### Sample inputs and expected outputs (`samples/`)
| file | what |
|---|---|
| `digits_noisy.csv` | input: 1,257 digit training rows (64 pixel features), 250 labels (19.9%) flipped to a confusable digit (seed 0) |
| `truth.csv` | the true label of every row, used only for scoring |
| `expected_issues.csv` | `audit` output: ranked rows with given label, suggested label, self-confidence and margin |
| `expected_audit_report.json` | flagged count, estimated noise rate, top confusions, per-class thresholds |
| `expected_score.json` | `score` output for the issues above |
| `expected_bench.json` / `expected_bench.txt` | full `bench` output (per-run and mean±std) |

Run on the sample, `audit` flags 245 rows and estimates the noise rate at 19.5% (true rate 19.9%).
Scored against the truth file: **precision 0.906, recall 0.888, F1 0.897**. The top estimated
confusions are 7→1, 8→3, 1→7, 6→5 and 5→6, which match the injected pairs.

## Measured results (`bench`, digits, random forest, 5 folds, 3 seeds, mean)
Detection F1 against the known flips:

| noise | CL prune_by_noise_rate | CL confident_joint | argmax ≠ label | p(label) < 0.5 | lowest-conf, oracle k* | random, same k | CL with in-sample probs (leaky) |
|---|---|---|---|---|---|---|---|
| asym 10% | **0.902** | 0.817 | 0.871 | 0.524 | 0.876 | 0.093 | 0.000 |
| asym 20% | **0.894** | 0.817 | 0.881 | 0.629 | 0.835 | 0.201 | 0.000 |
| asym 30% | 0.823 | 0.785 | **0.839** | 0.653 | 0.751 | 0.305 | 0.000 |
| uniform 10% | 0.890 | 0.806 | 0.895 | 0.532 | **0.939** | 0.106 | 0.000 |
| uniform 20% | 0.932 | 0.864 | 0.932 | 0.611 | **0.961** | 0.198 | 0.000 |
| uniform 30% | 0.941 | 0.873 | 0.945 | 0.647 | **0.966** | 0.302 | 0.000 |

\*The oracle-k baseline uses the *true* number of flips, so it is not a usable detector. It is an
upper reference for ranking by self-confidence.

Precision of CL prune_by_noise_rate vs argmax-disagreement: 0.905 vs 0.776 (asym 10%), 0.896 vs 0.805
(asym 20%), 0.784 vs 0.775 (asym 30%), and 0.97–0.99 vs 0.81–0.90 (uniform). The estimated noise rate
lands within 0.01 of the truth in every setting except asym 30% (estimate 0.333 vs true 0.300). The
noise-matrix MAE is ≤ 0.010.

Clean-test accuracy after retraining (538 test rows, labels never noised):

| noise | trained on noisy labels | pruned by CL | pruned by argmax | random prune | clean-label oracle |
|---|---|---|---|---|---|
| asym 10% | 0.968 | 0.966 | 0.960 | 0.966 | 0.971 |
| asym 20% | 0.951 | 0.959 | 0.956 | 0.947 | 0.971 |
| asym 30% | 0.884 | 0.936 | **0.946** | 0.845 | 0.971 |
| uniform 20% | 0.966 | 0.961 | 0.958 | 0.956 | 0.971 |

What the numbers show:
- **Leakage dominates.** Feeding confident learning in-sample random-forest probabilities finds **zero**
  flips in every run, because the forest memorises the wrong labels. Out-of-fold probabilities are the
  whole method.
- **The CL variants trade precision for recall.** `confident_joint` and `both` reach 0.97–1.00 precision
  at ~0.7 recall. `prune_by_noise_rate` balances the two. Use the strict variant to cut relabelling cost
  when each review is expensive.
- **The simple baseline is close.** "Model disagrees with the label" has the highest recall and matches
  or beats CL on F1 under uniform noise and at 30% asymmetric noise. Its precision is lower, so more of
  the rows it flags are false alarms. CL's distinctive outputs are the **noise-rate and confusion-matrix
  estimates**, which the baselines do not provide.
- **Pruning helps only when noise hurts.** A random forest is fairly robust to 10–20% noise on this
  dataset, so pruning changes test accuracy by about ±1 point there (within seed noise). At 30%
  asymmetric noise, both CL and argmax pruning recover most of the lost accuracy, and argmax does
  slightly better. These are 3-seed means on one small dataset, not significance tests.

## Design decisions
- **The split comes before noise.** A stratified 70/30 split is made first. Noise goes into the
  training labels only, so the test set's labels stay clean. No detector ever sees test rows. Each
  `bench` seed controls the split, the noise and the model, so every run is reproducible.
- **Noise is injected exactly**, not by sampling: `round(rate·n_c)` flips per class. That makes
  per-class rates and ground truth exact, and the asymmetric digit pairs mimic real annotator confusion.
- **Folds are capped at the smallest class size.** If a fold is missing a class, `predict_proba` is
  padded to all K classes, so rare classes cannot crash the audit.
- `prune_by_noise_rate` never prunes a class down to zero rows. A class that no row confidently
  joins gets an infinite threshold, and a class with no confident rows is treated as clean.
- **The baselines come with references.** The table includes a random baseline at the same k as a
  floor, and an oracle-k baseline as a ceiling for self-confidence ranking.
- **No external services.** The core algorithm needs only NumPy. Rerunning the CLI reproduces the
  committed sample outputs byte for byte, and the tests check this.

## Limitations
- One real dataset (digits) with *synthetic*, class-conditional noise. Real noise often depends on
  the example (ambiguous images, for instance). Confident learning assumes class-conditional noise,
  so on real data its estimates are approximations.
- Results depend on the quality and calibration of the probabilities. A weak model makes clean but
  hard rows look mislabeled.
- When the model systematically mispredicts a pair of classes, the confident joint calls that noise
  as well. This shows up as the overestimate at asym 30%.
- It works on tabular numeric features only. Text or images need an embedding step first, and the
  tool has no multi-label or regression support.
- Suggested labels are hints for a human reviewer, not automatic corrections.
- The tool does not ship the reference `cleanlab` package and is not checked against it. The
  formulas follow the paper.
