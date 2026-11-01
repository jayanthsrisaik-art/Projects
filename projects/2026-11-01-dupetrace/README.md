# dupetrace — duplicate bug-report detection with stack-trace normalisation

When a bug ships, the tracker fills with reports of the *same* defect written by
different people: different titles, different request ids and memory addresses,
line numbers from a different release, a trace pasted in full, truncated, or not
at all. Triage time goes on spotting "this is #1234 again".

dupetrace is a small service that, when a report is filed, suggests the earlier
reports it most likely duplicates, says *why* (same exception, same normalised
message, shared frames), and decides whether the match is strong enough to flag.
It combines:

* **data engineering** — validated, idempotent JSONL ingestion into SQLite with a
  quarantine table, parse-once derived columns, and a chronological replay that
  only ever looks at the past;
* **ML** — BM25 candidate generation over normalised text and frames, a
  logistic-regression reranker over pairwise similarity features, thresholds
  tuned on a held-out validation period, compared with three baselines;
* **software** — a FastAPI service and CLI that run the same code path that was
  evaluated.

Everything runs locally on seeded synthetic data. There are no paid services and
no LLM calls.

## Architecture

```
reports.jsonl ─► store.ingest ──► SQLite reports(+exc_type, norm_message, frames)
                 │  validate, idempotent by id + content hash, conflicts kept first-write
                 └► quarantine(line, reason)

query report ─► normalize.parse_report_text ─► BM25Index (past docs only) ─► top-50 candidates
                                                                         │
                         features.pair_features (18 similarity features) ◄┘
                                          │
                         model.Reranker (LogReg, JSON weights) ─► ranked suggestions
                                          │                       + reasons
                         threshold (chosen on validation) ─► likely_duplicate?
```

| module | role |
|---|---|
| `normalize.py` | masks URLs, e-mails, UUIDs, hex addresses, timestamps, paths, numbers; parses Python tracebacks and JS stacks into `dir/file:function` frames (innermost last, no line numbers); light tokeniser |
| `store.py` | schema, validation, idempotent ingest, quarantine, duplicate-link feedback table |
| `index.py` | incremental BM25 (IDF and avg length from documents added so far) plus a self-match score for normalisation |
| `features.py` | pairwise features (BM25 relative/self-normalised, title/message Jaccard, exception match/conflict, frame Jaccard, innermost-frame match, component match/conflict, time and version gap) and the human-readable reasons |
| `model.py` | standardised logistic regression, saved as plain JSON (no pickle) |
| `engine.py` | `DuplicateFinder`: candidates → rerank → threshold; used by API, CLI and evaluation |
| `evaluate.py` | chronological replay, baselines, metrics, threshold selection |
| `synth.py` | seeded synthetic tracker with hard negatives |
| `api.py` / `cli.py` | FastAPI app and command line |

## Setup

Python 3.10+.

```bash
cd projects/2026-11-01-dupetrace
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# 1. synthetic data: data/reports.jsonl + data/labels.csv (ground truth kept separate)
python -m dupetrace generate                      # 420 bugs, seed 7 -> 915 reports

# 2. ingest (run it twice: the second run reports 915 unchanged)
python -m dupetrace ingest data/reports.jsonl

# 3. chronological evaluation; writes artifacts/metrics.json and artifacts/reranker.json
python -m dupetrace evaluate

# 4. variance across synthetic seeds
python -m dupetrace sweep --seeds 1 2 3 4 5

# 5. suggestions for new reports (read-only)
python -m dupetrace ingest samples/existing_reports.jsonl --db data/sample.db
python -m dupetrace query samples/new_reports.jsonl --db data/sample.db --model samples/reranker.json

# 6. service
python -m dupetrace serve --db data/dupetrace.db --model artifacts/reranker.json
curl -s localhost:8000/health
curl -s -X POST localhost:8000/reports/preview -H 'content-type: application/json' \
     -d "$(head -1 samples/new_reports.jsonl)"
```

API:

| method | path | |
|---|---|---|
| `POST` | `/reports` | suggest duplicates, *then* store and index the report (201; 409 if id exists) |
| `POST` | `/reports/preview` | suggestions only, nothing stored |
| `GET` | `/reports/{id}` | stored report with parsed exception, message and frames |
| `POST` | `/reports/{id}/duplicate-of` | triager confirms a link `{ "target_id": ... }` (feedback table) |
| `GET` | `/health` | index size, whether a model is loaded |

Without a model file the service still works: it returns BM25-ordered
suggestions and never sets `likely_duplicate`.

### Samples

* `samples/existing_reports.jsonl` — six hand-written reports, including two
  *sibling* bugs (same title, same exception, same outer frame, different
  `KeyError` key and inner frame).
* `samples/new_reports.jsonl` — four new reports: a duplicate with a different
  install path, shifted line numbers and no component; a duplicate with no trace,
  only the message; an unrelated report; a truncated trace of the second sibling.
* `samples/bad_reports.jsonl` — rows that must be quarantined or flagged as conflicts.
* `samples/expected_suggestions.json` — output of the `query` command above.
  Q001 → S001 (0.989, flagged), Q004 → S002 (0.996, flagged — not its sibling
  S001), Q003 → no suggestions, Q002 → S003 ranked first but at 0.286, *below*
  the 0.850 threshold, so it is not flagged (see limitations).
* `samples/reranker.json` / `samples/expected_metrics.json` — model and metrics
  from the default `generate` + `evaluate` run; a test re-runs the pipeline and
  checks it reproduces these numbers.

## Evaluation

**Protocol.** Reports are replayed in arrival order. Each report first queries
against everything filed *before* it, then joins the indexes, so IDF statistics,
candidates and labels are exactly what a live system would have had. The
stream is cut by position: first 60% train, next 15% validation, last 25% test.
The reranker is fit only on candidate pairs from train-period queries; every
method's flag threshold is chosen to maximise F1 on validation queries; all
numbers below are from the test period. A query is a duplicate query if any
earlier report belongs to the same latent bug, and any such earlier report
counts as a correct match. Features are similarity measures only (no ids), so
the model cannot memorise bugs.

**Methods.**
* `signature_exact` — exception + innermost three frames (or exception +
  normalised message when there is no trace) must match exactly; newest first.
  This is what many crash-grouping tools do.
* `bm25_raw` — BM25 over raw title + body tokens.
* `bm25_normalised` — BM25 over normalised tokens, frames and exception type
  (the candidate generator).
* `reranker` — logistic regression over the top-50 normalised candidates.

**Metrics.** Recall@k and MRR over test duplicate queries (ranking quality);
for the flag decision, a flag is correct only if the query really is a
duplicate *and* the top-1 suggestion is one of its earlier reports.
Precision = correct flags / flags, recall = correct flags / duplicate queries,
FA(new) = share of genuinely new reports that were flagged.

**Measured results** (default data: 420 bugs, seed 7 → 915 reports; test
period 2026-07-10 … 2026-09-01, 229 queries of which 139 duplicates, 29 of those
without a stack trace; 26,175 training pairs, 752 positive):

```
candidate recall@50 (normalised BM25): 0.971

method               R@1    R@5   R@10    MRR |   prec recall     F1 FA(new)
signature_exact    0.496  0.504  0.504  0.499 |  0.958  0.496  0.654   0.011
bm25_raw           0.748  0.878  0.899  0.804 |  0.944  0.489  0.645   0.022
bm25_normalised    0.863  0.928  0.950  0.895 |  0.915  0.698  0.792   0.078
reranker           0.964  0.971  0.971  0.968 |  1.000  0.842  0.914   0.000

Recall@1 by query type (duplicate queries):
  signature_exact   with trace 0.618   without trace 0.034
  bm25_raw          with trace 0.827   without trace 0.448
  bm25_normalised   with trace 0.909   without trace 0.690
  reranker          with trace 0.964   without trace 0.966
```

The reranker's Recall@1 (0.964) is at the candidate generator's ceiling
(recall@50 = 0.971): on this data the remaining misses are retrieval misses,
not ranking misses. The "without trace" figure is over only 29 queries.

Seed sweep (`python -m dupetrace sweep`), Recall@1 / F1 on each seed's test period:

```
seed  signature_exact   bm25_raw      bm25_normalised   reranker
   1  0.474/0.612       0.674/0.598   0.807/0.675       0.904/0.861
   2  0.575/0.713       0.750/0.653   0.831/0.781       0.931/0.917
   3  0.545/0.692       0.739/0.627   0.806/0.740       0.925/0.906
   4  0.516/0.670       0.667/0.596   0.762/0.693       0.929/0.891
   5  0.476/0.627       0.678/0.608   0.839/0.727       0.958/0.941
```

The ordering of the methods is the same on every seed; the reranker's Recall@1
ranges 0.904–0.958.

Largest standardised reranker weights (default run): `msg_both` −2.44,
`frames_both` −2.37, `msg_jaccard` +1.68, `log_days_gap` −1.53, `bm25_self` +1.47,
`exc_conflict` −1.30, `exc_match` +1.20, `frame_jaccard` +1.02. The negative
"both present" weights act as missing-value corrections: when both reports
carry a message or trace, the model asks the matching features to do the work
instead of giving credit for presence alone.

## Design decisions

* **Normalise before you retrieve.** Raw BM25 rewards sharing a UUID or a hex
  address, which are rare tokens with high IDF. Masking them and turning frames
  into `dir/file:function` tokens moves Recall@1 from 0.748 to 0.863 without
  any learning.
* **Exact signatures are precise but brittle.** `signature_exact` is right 96%
  of the time it fires, but it fires for only half the duplicates: a truncated
  trace, an extra outer frame or a pasted message instead of a trace breaks the
  key.
* **Retrieve, then rerank.** BM25 keeps the candidate set small (50) and cheap
  to update incrementally; the reranker only has to order 50 items, so it can
  use pairwise features that would be too costly over the whole corpus.
* **Corpus-size-robust features.** An early version used `log(BM25)`. Absolute
  BM25 scores depend on corpus size, so in the six-report sample database the
  correct match for Q002 scored only 0.019. The feature was replaced by the
  score divided by the query's self-match score; Q002 now scores 0.286. That is
  still below the threshold (see limitations).
* **Thresholds from validation, per method.** Each method gets its own
  F1-optimal threshold from the validation period, so the comparison is not
  biased by one method's score scale.
* **Suggest before insert.** `POST /reports` computes suggestions before the
  report joins the index, so a report can never be suggested as its own duplicate,
  and the API matches the offline replay.
* **JSON model file.** Twenty numbers do not need pickle; JSON is reviewable and
  safe to load, and loading checks the feature list and rejects non-finite values.
* **Ground truth in a separate file.** The generator writes `labels.csv`
  separately from `reports.jsonl`; nothing in ingestion or features can read it.

## Limitations

* **Synthetic data.** The generator's vocabulary, paraphrases, trace formats and
  arrival process are invented. Absolute numbers will not transfer to a real
  tracker; the comparison between methods is the useful part. In particular,
  `log_days_gap` gets a large weight because generated duplicates mostly arrive
  within days — real arrival patterns may differ.
* **Conservative on trace-less reports.** In the sample, Q002 (message only)
  ranks the right report first but scores 0.286, below the validation
  threshold, so it is not flagged. The threshold optimises F1 over all
  queries; a per-query-type threshold would flag more of these.
* **Retrieval ceiling.** About 3% of test duplicates never reach the top 50 BM25
  candidates; the reranker cannot recover them.
* **Single process.** The index is in memory and rebuilt from SQLite at startup;
  writes are serialised by a lock. Fine for thousands of reports, not for a
  large multi-instance deployment.
* **Two trace formats.** Python tracebacks and V8-style JS stacks only (no
  Java, Go or native crash dumps), and the tokeniser is English-only.
* **Feedback is stored, not used.** Triager links go into `duplicate_links`;
  retraining on them is not implemented.

## Tests

```bash
python -m pytest -q
```

36 tests covering normalisation and trace parsing, idempotent ingest /
quarantine / conflicts, BM25 behaviour (past-only IDF, recency tie-break, self
score), features, model round-trip and validation, hand-computed metrics and
threshold selection, a no-future-leakage check on the replay, baseline ordering
on a small synthetic run, exact reproduction of `samples/expected_metrics.json`,
the API (create/preview/409/422/404, sibling disambiguation, no-model mode) and
the CLI (query output equals `samples/expected_suggestions.json`, end-to-end
generate → ingest → evaluate).
