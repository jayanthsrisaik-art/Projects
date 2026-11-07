# semguard: a semantic cache for LLM calls that knows when *not* to reuse an answer

A semantic cache saves LLM calls by reusing the answer to a *similar* earlier prompt. With a
plain cosine-similarity threshold, the most similar prompts are often the wrong ones to reuse:

| cached prompt | new prompt | cosine (hashing embedder) | correct to reuse? |
|---|---|---|---|
| I want to turn **off** two-factor authentication | I want to turn **on** two-factor authentication | 0.90 | no |
| business class policy for **4**-hour flights | business class policy for **6**-hour flights | 0.84 | no |
| what's the weather in london | what's the weather in london **today** | 0.83 | no (stale) |
| How do I reset my password? | help me reset my password please | 0.99 | yes |

semguard puts a set of cheap, explainable **guards** after the similarity search. Each guard can veto a
candidate. It also measures, on a labelled query stream, how often each policy serves a wrong answer.

## Architecture

```
prompt ─► scope = sha256(model, system prompt, tenant, params)
       ─► facts = extract(prompt)       literals (numbers, ids, e-mails, URLs, "quoted")
       │                                negation · polarity (enable/disable, import/export…)
       │                                domain terms (python/java, pro/basic plan, csv/json…) · volatile words
       ├─ volatile? ─────────────────────────► BYPASS (call model, do not store)
       ├─ exact normalised match in scope? ──► HIT
       └─ top-k cosine candidates ≥ threshold, best first
              for each: expired? guard conflict?  → reject with a reason, try the next one
              first compatible candidate ───────► HIT        none ─► MISS (call model, store)
```

| module | role |
|---|---|
| `semguard/text.py` | NFKC/lower-casing, contraction expansion, id-preserving tokeniser (`ord-4521`, `a@b.com`, `3.5`) |
| `semguard/embed.py` | offline embedder: signed feature hashing of words, content bigrams and char 3–5-grams (NumPy) |
| `semguard/facts.py` + `default_lexicon.json` | fact extraction and `conflict(a, b, guards)` that returns a reason string |
| `semguard/cache.py` | `SemanticCache`: SQLite storage, in-memory per-scope matrix, exact + top-k guarded lookup, TTL, LRU eviction, counters |
| `semguard/llm.py` | `MockLLM` (deterministic, offline) and optional `AnthropicLLM` |
| `semguard/service.py` | FastAPI: `POST /v1/complete`, `GET /v1/stats`, `DELETE /v1/cache`, `GET /healthz`; `X-Cache: HIT/MISS/BYPASS` header |
| `semguard/datagen.py` | synthetic labelled query stream (32 intent families in 11 topics) |
| `semguard/evaluate.py` | stream replay, topic-grouped dev/test split, dev-only threshold calibration, ablations |

## Setup

```bash
cd projects/2026-11-07-semguard
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

Python 3.11+ is required. It was tested with Python 3.11, NumPy 2.4, FastAPI 0.162 and pytest 9.2.

## Usage

```bash
# Why would (or wouldn't) a cached prompt answer a new one?
python -m semguard check "can I get a refund for order 4521" "can I get a refund for order 4512"
#  -> {"similarity": 0.7161, "verdict": "miss", "reason": "literals differ (num:4521 vs num:4512)", ...}

# Send prompts through a persistent cache (mock LLM by default)
python -m semguard ask --db demo.db "How do I reset my password?" "help me reset my password please"

# Run the API (mock backend; SEMGUARD_BACKEND=anthropic + ANTHROPIC_API_KEY for a real model)
python -m semguard serve --port 8000
curl -s -XPOST localhost:8000/v1/complete -H 'content-type: application/json' \
     -d '{"prompt": "help me reset my password please", "system": "", "tenant": "acme"}'

# Reproduce the evaluation (about 20–30 s)
python -m semguard eval --json report.json            # add --timing for lookup latency
python -m semguard gen-data --n 3000 --seed 7 --out stream.jsonl && python -m semguard eval --data stream.jsonl
```

Environment variables: `SEMGUARD_DB` (default `semguard.db`), `SEMGUARD_THRESHOLD` (default `0.68`),
`SEMGUARD_BACKEND` (`mock`|`anthropic`), `SEMGUARD_MODEL` (model id for the Anthropic backend; default `claude-haiku-5-5`).
The Anthropic backend needs `pip install anthropic`. It was not exercised in this run because no API key was
available. Everything else runs in mock mode.

### Samples and expected outputs

* `samples/check_examples.json` has 14 hand-written prompt pairs with the expected verdict (`hit`/`miss`/`bypass`)
  and the reason. These are the evaluation examples for the guard logic. They include one *known failure*: a
  paraphrase with few shared words that the lexical embedder misses. Each pair is a parametrised test.
* `samples/queries_sample.jsonl` holds the first 40 queries of the synthetic stream, with their labels.
* `samples/ask_session.jsonl` is the output of `ask` for 7 prompts against the mock LLM.
* `samples/expected_eval.json` / `.md` contain the full evaluation report for `--n 3000 --seed 7`. A test re-runs
  the evaluation and compares it with this file.

## Evaluation

**Data.** The data is a synthetic stream of 3,000 support, coding, HR and API questions. Each query carries a
ground-truth key, `(intent family, slot values)`. A cache hit is **correct** only if the served entry has the same
key and the intent is not time-sensitive. A hit on a time-sensitive intent ("rate today", "order status right now")
counts as wrong because the answer is stale. Near-misses are built into the data:
* slot changes inside a family (order 4521 vs 4512, error 404 vs 500, pro vs basic plan, python vs java)
* sibling families that differ in polarity (enable/disable, import/export, read/write, ascending/descending,
  lock/unlock, upgrade/downgrade), in object (password/username/email) or in negation (can't log in / how to log in)
* paraphrase templates, filler prefixes, suffixes, random casing, and an 8% chance of one transposition typo

**Split.** Whole topics go to either dev or test (a topic-grouped split), so sibling near-misses never
straddle the split. The threshold is chosen on intents the test set has never seen. Dev has 4 topics
(1,043 queries) and test has 7 topics (1,957 queries). Both sides contain time-sensitive intents.

**Calibration.** For each policy, the threshold is the lowest value on a 0.50–0.99 grid whose **dev** hit
precision is ≥ 0.99. A test checks that changing test labels does not change the chosen threshold.

**Metrics.** The metrics are computed on test only.
* *hit precision* is correct hits / hits.
* *recall of reusable* is correct hits / queries whose key had already appeared, i.e. what an oracle cache could
  have answered.
* *wrong hits* are wrong answers served, broken down by kind.

Hit rate on its own is misleading here because it rewards serving wrong answers.

### Results (measured, `python -m semguard eval`, seed 7)

| policy (test split) | threshold | hit rate | hit precision | recall of reusable | wrong hits | wrong by kind |
|---|---|---|---|---|---|---|
| exact_match (normalised) | exact | 0.547 | 0.790 | 0.573 | 225 | stale_volatile 225 |
| semantic_only | 0.99 (target unmet on dev) | 0.738 | 0.787 | 0.772 | 307 | stale_volatile 307 |
| semantic_plus_volatile | 0.86 | 0.619 | 0.951 | 0.781 | 60 | sibling_polarity 49, slot_value 10, stale 1 |
| **guarded** (all guards) | 0.68 | 0.683 | **0.985** | **0.893** | **20** | sibling_polarity 18, stale 2 |

At the same threshold, on the test split:

| threshold | semantic_only precision / wrong | guarded precision / wrong |
|---|---|---|
| 0.68 | 0.528 / 849 | 0.985 / 20 |
| 0.80 | 0.672 / 530 | 0.994 / 7 |
| 0.90 | 0.765 / 354 | 0.993 / 8 |

Ablation: one guard removed at a time, at the guarded threshold of 0.68.

| removed guard | dev precision / wrong | test precision / recall / wrong |
|---|---|---|
| none (guarded) | 0.991 / 6 (639 dev hits) | 0.985 / 0.893 / 20 |
| literals | 0.863 / 91 | 0.903 / 0.832 / 132 |
| negation | 0.947 / 34 | 0.985 / 0.893 / 20 |
| polarity | 0.975 / 16 | 0.848 / 0.780 / 206 |
| terms | 0.836 / 110 | 0.842 / 0.785 / 218 |
| volatile | 0.729 / 235 | 0.776 / 0.893 / 380 |

The figures for the unablated "none (guarded)" row come from the `dev` section of `samples/expected_eval.json`.

What the numbers say:
* **A threshold alone cannot reach 99% precision on this data.** Stale answers to time-sensitive questions have
  near-identical text, and slot near-misses score as high as paraphrases. Even at 0.99, semantic-only serves
  307 wrong answers on test.
* **The guards let the threshold drop to 0.68** and still serve fewer wrong answers. Recall of reusable
  queries goes from 0.57 for exact match to 0.89. Each guard except negation removes a distinct class of error.
  The negation guard matters only on dev, where the "can't log in" / "how to log in" pair lives.
* **The calibrated target did not fully transfer.** Dev precision was 0.991, but test precision was 0.985,
  below the 0.99 target. The remaining test errors are guard misses, all listed in `wrong_by_kind`:
  * word-order polarity: "sort a list from *largest to smallest*" vs "*smallest to largest*"
  * typos inside guard words: "epxort", "curernt", "tdoay"
* Mean lookup latency measured 0.05–0.10 ms per query over the 1,957-query test replay (`--timing`; depends on the machine).

Development note: the dev split was used for iteration. The unit-glued number fix (`6-hour` → `num:6`) came from a
dev error. The test-split errors above were inspected but deliberately *not* fixed, for example by adding
"largest to smallest" to the lexicon, because that would tune the method on the test set.

## Design decisions

* **The guards veto candidates; they do not re-rank them.** Lookup takes the top-k candidates, best first, and serves
  the first one that passes every guard. A rejected near-miss (`turn off 2fa`) therefore does not hide a compatible
  paraphrase with a lower score. Rejections, with their reasons, are returned in the API response.
* **Literals must match as exact sets.** If one prompt mentions order 4521 and the other mentions no order, they do
  not share an answer. Tokens with a single digit (`2fa`, `s3`, `mp3`) are treated as words, and `no.` before a
  number is not read as a negation.
* **Polarity and term groups only conflict when both sides mention the group.** A paraphrase may leave out "in
  python", so silence on one side is allowed. This is a deliberate recall/precision trade-off.
* **Time-sensitive prompts are never cached** (bypass) rather than given a short TTL. With a short TTL the
  correctness of a reuse depends on what the question is about, which the cache cannot know.
* **Scope is part of the key.** Answers are reused only under the same model, system prompt, tenant and params.
* **The embedder is pluggable and offline by default.** The hashing embedder needs no model download and is
  deterministic, which keeps the tests and the expected report stable. Any `embed(text) → unit vector` can replace it.
* **SQLite holds the durable copy and a NumPy matrix holds the search index.** Brute-force cosine search is enough
  for tens of thousands of entries per scope. The index is rebuilt from SQLite on start and after eviction.

## Limitations

* **The data is synthetic and written by the same author as the lexicon.** The guard vocabularies cover most
  near-misses in the data by construction. The topic-grouped split and the ablation keep this visible, but the
  absolute precision numbers are optimistic for real traffic. Domain terms must be configured for your domain
  (`Lexicon.from_file`).
* **The lexical embedder misses paraphrases with few shared words.** For example, "Python code to load a CSV" vs
  "how do I read a csv file in python" has cosine 0.48 and is a miss (shown in `check_examples.json`). A neural
  sentence embedder would raise recall, and the threshold would then have to be recalibrated.
* **The guards are lexical.** They miss typos in guard words, word-order meaning ("A to B" vs "B to A",
  "largest to smallest"), paraphrased negation ("unable to") and antonyms that are not in the lexicon. They also
  over-block when a paraphrase adds a negation ("what happens to days I *don't* use").
* **The time-sensitive check is keyword-based.** "Where is my order?" with no time word is cached. The `cache: false`
  request flag is the escape hatch.
* **The cache runs in a single process.** Each process has its own in-memory index, and there is no cross-process
  invalidation. The Anthropic backend is implemented but was not exercised in this run.
