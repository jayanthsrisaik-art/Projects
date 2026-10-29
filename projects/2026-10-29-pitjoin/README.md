# pitjoin: point-in-time correct feature joins

Builds ML training sets from timestamped feature tables **without leaking the
future**, measures how much a naive join leaks, and serves the same features
from an incrementally materialized online store with a training/serving skew
check. Uses only the Python standard library and SQLite.

## Problem

A training row for "was transaction T fraudulent?" must contain the features
the model could have seen **when T happened**. The usual shortcuts get this
wrong in two different ways:

1. **Joining the latest value** (`labels.merge(latest_features, on="customer_id")`)
   attaches facts from after the label: *future events*.
2. **An as-of join on event time** (`pandas.merge_asof`, and the default in many
   feature stores) fixes that, but still uses rows that had **not landed yet**.
   A daily aggregate for the window ending at midnight is computed at 06:00,
   batches land days late, and corrections are backfilled a week later. At
   03:00 the model saw yesterday's value, not today's.

`pitjoin` joins **bitemporally**: a feature row is eligible at label time `T`
only if `event_ts <= T` **and** `created_ts <= T`, and only if it is not older
than the view's TTL.

## Semantics

For each entity row `(key, T)` and each feature view:

```
eligible(f) := f.key == key
               and f.event_ts <= T                          # no future facts
               and (ttl is None or f.event_ts >= T - ttl)   # not stale (inclusive)
               and (mode == "event" or f.created_ts <= T)   # already ingested
chosen      := argmax over eligible rows of (event_ts, created_ts, file position)
```

* `mode="knowledge"` (default) is the bitemporal rule above. `mode="event"`
  ignores `created_ts` for eligibility; it exists so its leakage can be measured.
* No eligible row means nulls for that view's features. Entity rows keep their
  order and duplicates. A null or unknown key matches nothing.
* Timestamps are ISO-8601. Offsets are honoured, naive values are treated as UTC,
  and everything is compared as integer UTC microseconds, so boundaries are exact.
* A view without `created_timestamp_field` uses `created = event_ts`.

## Architecture

```
feature_repo.json ──► repo.py  (typed loading, validation with line numbers)
                         │
          ┌──────────────┼───────────────────────┐
          ▼              ▼                       ▼
   engines.py      training.py             online.py
   sweep  (prod)   build_training_set      materialize (incremental, watermark)
   sql    (SQLite) leakage_report          get_online  (TTL at serve time)
   oracle (O(n·m)) write_csv               skew_check  (online == offline @ watermark)
   naive_latest / asof_event_only baselines
```

* **`sweep_join`**, O((n+m) log(n+m)). Both time constraints have the form
  `x <= T`, so a feature row becomes eligible at `available_at = max(event_ts, created_ts)`
  and stays eligible as `T` grows. Walking labels in time order and admitting
  rows by `available_at` turns the argmax into a **running maximum**. The TTL
  needs no extra state: if the newest eligible event is too old, every other
  one is older.
* **`sql_join`** expresses the same join as warehouse SQL: a range join on key
  and time, then `ROW_NUMBER() OVER (PARTITION BY label ORDER BY event_ts DESC, created_ts DESC, idx DESC)`.
* **`oracle_join`** is the definition written as a brute-force loop. Tests
  check that the other two engines agree with it on randomized data.
* **Online store** (SQLite): each `materialize --end W` admits rows with
  `available_at` in `(previous watermark, W]`. An upsert keeps the higher-ranked
  row, so any sequence of runs ending at `W` gives the same state as one run,
  and lookups equal the offline join at `W`. `skew-check` verifies that per key.

## Setup

```bash
cd projects/2026-10-29-pitjoin
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt                     # only pytest; runtime is stdlib
```

## Usage

```bash
# Build a training set (the sample output is examples/expected_training_set.csv)
python -m pitjoin join examples/feature_repo.json examples/transactions.csv --out train.csv
python -m pitjoin join ... --engine sql            # same result via SQLite
python -m pitjoin join ... --mode event            # leaky event-time-only join, for comparison

# How much would naive joins have leaked? (sample: examples/expected_leakage.json)
python -m pitjoin leakage examples/feature_repo.json examples/transactions.csv

# Online store
python -m pitjoin materialize examples/feature_repo.json --db online.db --end 2026-03-04T12:00:00Z
python -m pitjoin get examples/feature_repo.json --db online.db --view customer_daily C1 C2
python -m pitjoin skew-check examples/feature_repo.json --db online.db   # exit 1 on skew
python -m pitjoin materialize ... --full                                  # rebuild

# Synthetic data and an end-to-end check
python -m pitjoin synth /tmp/fs --seed 7
python -m pitjoin demo

# Tests
python -m pytest -q
```

Repository format (`examples/feature_repo.json`): each view has `name`,
`entity` (the join-key column, shared with the entity file), `source` (CSV,
relative to the repo file), `timestamp_field`, optional
`created_timestamp_field` and `ttl_seconds`, and `features` with dtypes
`int|float|str|bool`. Output columns are named `<view>__<feature>`.

### The worked example

`examples/` has 9 labelled transactions chosen to exercise each rule.
`expected_training_set.csv` was checked by hand:

| txn | what happens | customer_daily |
|-----|--------------|----------------|
| T1 03-01 03:00 | the 03-01 row lands at 06:00, so it is hidden | null |
| T2 03-01 12:00 | now it is visible | 3 |
| T4 03-04 12:00 | a correction for 03-03 lands on 03-05 09:00, so the original is used | 4 |
| T5 03-05 12:00 | the correction is now visible | 6 |
| T6 03-07 00:00 | the newest row (03-03) is older than the 3-day TTL | null |
| T7 03-01 12:00 | C2's 03-01 batch arrived late (03-04) | null |
| T3 | `merchant_risk` row at exactly T is included; the `+02:00` row is older | 0.30 |

On this data, the event-time as-of join is wrong on T1, T4 and T7 (3/9 rows).
The latest-value join is wrong on 8/9.

## Measured results (`python -m pitjoin demo`, seed 7)

Synthetic data: 300 customers × 60 days of daily aggregates (18,843 rows
including corrections), 772 merchant risk updates, 590 profile rows, and 5,000
labelled transactions. Run on the container this was built in:

| check | result |
|-------|--------|
| sweep vs SQL, all 5,000 rows, 3 views × 2 modes | identical |
| sweep vs brute-force oracle, first 400 rows | identical |
| sweep time, largest view (customer_daily) | ~20 ms |
| SQLite time, same | ~65–80 ms |
| **latest-value join**: label rows with ≥1 wrong feature | 4,991 / 5,000 (99.8%) |
| **event-time as-of join**: label rows with ≥1 wrong feature | 1,660 / 5,000 (33.2%), all "not yet ingested" |
| online store after 6 incremental runs vs offline join | 0 mismatches over 680 keys |

The as-of join's 33% comes mostly from the 06:00 landing time of daily
batches: any label between midnight and the batch landing sees a value that
did not exist yet. Late batches and corrections add the rest. Timings are
single runs and vary from machine to machine.

## Tests

`python -m pytest -q`: **121 tests**, about 2 s. They cover:
* timestamp parsing (offsets, microsecond exactness) and repo validation errors with line numbers;
* the example file, byte for byte, for all three engines, plus each rule spelled out;
* boundaries: inclusive `T`, inclusive TTL edge, `created < event_ts`, ties, null/unknown keys;
* 40 randomized cases where sweep, SQL, oracle (both modes) and as-of (event mode) must agree;
* leakage counts on the example;
* online store: incremental runs equal one-shot, a watermark cannot move backwards,
  `--full` rebuilds, TTL is applied at serve time, 25 random materialization
  schedules match the offline join, and a skew-check catches a backdated row;
* CLI commands, exit codes, synthetic-data determinism and the demo.

I also ran mutation checks by hand. Admitting rows by event time only in
knowledge mode fails 41 tests, and making the SQL TTL boundary exclusive fails
23. Removing `idx` from the SQL tie-break was **not** caught: SQLite's
`(k, ts)` index already returns ties in rowid order. The explicit tie-break is
kept so the result does not depend on that.

## Design decisions

* **Bitemporal by default.** The safe rule is the default. The leaky one has
  to be requested by name.
* **Engines return row indices, not values.** Results compare exactly (floats
  and nulls included), provenance is kept, and leakage can be classified per
  pair (future event / not yet known / stale).
* **Three implementations of one definition.** The Python sweep is fast. The
  SQL version shows how to run the join in a warehouse. The oracle is the
  definition itself, and the tests hold the other two to it.
* **Deterministic tie-break** on `(event_ts, created_ts, file position)`, so a
  later correction with the same event time wins once it has landed.
* **TTL applies at read time** both offline and online, so expiry is handled
  the same way in both.
* **Integer microseconds** instead of floats or strings: no rounding at
  boundaries, and mixed offsets compare correctly.

## Limitations

* Sources are CSVs read fully into memory. This is fine for samples and tens of
  thousands of rows, not a warehouse-scale engine. The SQL join is a range join
  without bucketing and would need partitioning on large data.
* Incremental materialization assumes `created_ts` is truthful and sources are
  append-only (`idx` = file position). A row that is backdated behind the
  watermark is missed until `--full`. `skew-check` detects this but does not
  repair it.
* One join key per view (no composite keys). A null feature value in the
  chosen row stays null and is not filled from older rows.
* No on-demand or derived features. Features are precomputed in the source tables.
* The online store is a local SQLite file, not a networked low-latency service.
* Synthetic data only. Leakage percentages describe this generator's ingestion
  delays, not any real system.
