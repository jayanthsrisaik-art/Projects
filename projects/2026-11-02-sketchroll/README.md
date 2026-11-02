# sketchroll: metric rollups you can actually re-aggregate

Most metrics pipelines keep one pre-aggregated row per hour, for example
`distinct_users`, `p95_latency` and `requests`. Then someone asks for "weekly
active users" or "p99 latency for the whole month", and the dashboard *sums*
the hourly distinct counts and *averages* the hourly percentiles. Both answers
are wrong. A user who shows up in 40 different hours gets counted 40 times, and
the average of p95s is not the p95 of the union. You would have to re-scan the
raw logs to get it right, and those may already be deleted.

sketchroll stores each hourly cell as **mergeable sketches** next to the exact
counters:

* **HyperLogLog** (p=12, 4 KiB of registers, ~1.6% standard error) for distinct
  users. Union is element-wise `max`, so the sketch of any range is exactly the
  merge of its hourly sketches.
* **DDSketch** (α = 1%) for latency quantiles. Bucket counts just add, so every
  quantile answer stays within 1% relative error of the true order statistic
  after any number of merges.
* exact `n`, `errors`, `sum`, `min`, `max`.

On top of the sketches sits a small data-engineering pipeline. It does
idempotent batch ingestion with a quarantine table, compacts old hours into day
rows, routes late-arriving data into already-compacted days, refuses queries it
cannot answer exactly, and has an invariant checker. A benchmark compares
everything against the exact answers computed from raw events and against the
naive sum/avg rollup.

Runtime code uses only the Python 3.10+ standard library (`sqlite3`, `hashlib`,
`zlib`). The data is seeded and synthetic, and no services are needed.

## Measured results

`python -m sketchroll bench` uses 14 days, 210,000 events (4,219 delivered
late, after compaction), 5 endpoints and 54 queries: windows from 1 hour to 14
days, per endpoint and across all endpoints. The output is checked in as
`samples/expected_bench.txt`.

| relative error vs exact | sketch median | sketch max | naive median | naive max |
|---|---|---|---|---|
| distinct users | 0.75% | 3.08% | 38.79% | 871.00% |
| latency p50 | 0.45% | 0.98% | 0.39% | 19.20% |
| latency p95 | 0.37% | 0.98% | 2.83% | 24.64% |
| latency p99 | 0.53% | 0.99% | 10.10% | 46.36% |

* 100% of sketch quantile answers were within α = 1%. For the naive rollup the
  share was 69% (p50), 15% (p95) and 11% (p99).
* Requests, errors, latency sum and max matched the exact values in every query,
  including after compaction and the late batch. No invariant violations.
* Redelivering the late batch was detected and skipped.
* Storage: 21.9 MB of raw JSONL became 300 rollup rows, with 132 KB of HLL blobs
  and 78 KB of DDSketch blobs (60 compacted day rows plus 240 hour rows for the
  last 2 days).
* The naive baseline's hourly cells are themselves *exact*, late data included.
  All of its error comes from re-aggregation. Its median p50 error is slightly
  lower than the sketch's because a weighted mean of hourly medians happens to
  sit near the true median for this unimodal data. Its tail percentiles and
  distinct counts are far off.

## Architecture

```
daily JSONL batches ──► store.ingest_file ──┬─► batches (ledger: batch_id = sha256, accepted/rejected)
   events.parse_event   validate, UTC,      ├─► quarantine (line, reason, raw)
                        group by (endpoint, hour)
                                            └─► rollups(endpoint, grain, start,
                                                        n, errors, lat_sum, lat_min, lat_max,
                                                        hll BLOB, dds BLOB)
                     store.compact(before) ──► hour rows of complete days → 1 day row (exact merge)
late batch ─► ingest: if the day is already compacted, merge into the day row
query(start, end, endpoints) ─► merge covering day + hour rows ─► Rollup.summary()
check_invariants ─► hour/day exclusivity, sketch count == n, ledger == Σn
```

| module | role |
|---|---|
| `hll.py` | HyperLogLog with a 64-bit blake2b hash (stable across processes), Ertl's improved estimator (no bias tables, no linear-counting switch), zlib-compressed serialisation |
| `ddsketch.py` | log-bucket quantile sketch, zero bucket, low-end collapse at `max_bins`, JSON+zlib serialisation |
| `rollup.py` | one cell: exact counters + both sketches, `merge`, `summary` |
| `events.py` | event schema, strict validation (timezone required, HTTP status, finite latency) |
| `store.py` | SQLite schema, atomic idempotent ingest, compaction, range query, invariant checker |
| `oracle.py` | exact answers from raw events and the naive sum/avg rollup baseline |
| `synth.py` | seeded logs: Zipf-like returning users, diurnal traffic, log-normal latency, incident hours, late events |
| `bench.py` / `cli.py` | end-to-end benchmark and command line |

## Setup

```bash
cd projects/2026-11-02-sketchroll
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt     # only pytest; runtime is stdlib
python -m pytest -q
```

## Usage

```bash
# synthetic data: one file per day plus late.jsonl
python -m sketchroll generate --out data --days 14 --events-per-day 15000 --seed 7

python -m sketchroll ingest  --db rollups.db data/events-*.jsonl
python -m sketchroll compact --db rollups.db --before 2026-10-13T00:00:00Z
python -m sketchroll ingest  --db rollups.db data/late.jsonl        # merges into compacted days
python -m sketchroll query   --db rollups.db --start 2026-10-01T00:00:00Z --end 2026-10-15T00:00:00Z
python -m sketchroll query   --db rollups.db --start 2026-10-14T18:00:00Z --end 2026-10-15T00:00:00Z \
                             --endpoint /checkout --endpoint /cart
python -m sketchroll check   --db rollups.db
python -m sketchroll bench   [--json]
```

### Samples

`samples/` holds a small 2-day dataset (`events-2026-10-0{1,2}.jsonl`, 790
events), `late.jsonl` (10 events), `bad_batch.jsonl` (9 lines, 7 invalid in
different ways) and the expected outputs of this sequence:

```bash
python -m sketchroll ingest  --db s.db samples/events-2026-10-01.jsonl samples/events-2026-10-02.jsonl \
                             samples/bad_batch.jsonl samples/events-2026-10-01.jsonl   # last one: skipped_duplicate
python -m sketchroll compact --db s.db --before 2026-10-02T00:00:00Z
python -m sketchroll ingest  --db s.db samples/late.jsonl
python -m sketchroll query   --db s.db --start 2026-10-01T00:00:00Z --end 2026-10-03T00:00:00Z
#   -> samples/expected_query_all.json
python -m sketchroll query   --db s.db --start 2026-10-02T10:00:00Z --end 2026-10-02T12:00:00Z --endpoint /cart
#   -> samples/expected_query_cart_window.json
python -m sketchroll query   --db s.db --start 2026-10-01T06:00:00Z --end 2026-10-01T12:00:00Z
#   -> exit 2: "day 2026-10-01 is compacted to daily grain; ..."
```

A test re-runs this sequence and compares against the JSON files.

## Tests (38)

* **HLL**: error within 4σ from 0 to 60k distinct values; duplicates don't change
  the sketch; the merge of parts is byte-identical to the sketch of the union
  (and commutative); serialisation round-trip; the hash value is pinned.
* **DDSketch**: the relative-error guarantee on log-normal plus Pareto data for
  11 quantiles × 5 seeds; zero bucket; merge equals the combined sketch;
  collapse caps the bins while upper quantiles keep the guarantee; invalid input.
* **Store**: re-ingesting a file is a no-op; a reused batch id with different
  content is rejected; each kind of bad row is quarantined with a reason;
  offsets are normalised to UTC; exact counters and sketch bounds hold against
  the oracle; compaction gives identical answers for day-aligned queries and
  only touches complete days; late data after compaction ends in the same state
  as ingest-then-compact; partial queries over compacted days are refused; a
  batch that fails mid-write rolls back completely and can be retried; the
  invariant checker catches corruption; sample outputs reproduce.
* **Bench/CLI**: the generator is deterministic and has returning users;
  sketches stay within bounds while the naive rollup is far off; the CLI works
  end to end, including exit codes.

## Design decisions

* **Exact counters and sketches side by side.** Counts, sums, min and max are
  exactly mergeable, so they are never approximated.
* **Batch ledger keyed by content hash.** HLL is idempotent under re-adding but
  counters and DDSketch are not, so retry safety comes from the ledger, written
  in the same transaction as the rollups.
* **Hour rows *or* a day row per (endpoint, day), never both.** Late data for a
  compacted day merges straight into the day row, so a query never
  double-counts or needs to reconcile two grains.
* **Refuse instead of guess.** A range that cuts through a compacted day raises
  `GranularityError` rather than answering with day-level data. Bounds must be
  on the hour.
* **Ertl's estimator** instead of classic HLL with bias tables. It is simpler to
  get right and accurate across the whole range (measured above).
* **Lower order statistic** (`sorted(x)[floor(q·(n-1))]`) as the quantile
  definition everywhere, so the DDSketch guarantee can be tested exactly.
* **Stdlib only.** Python is the right size for this scale of data and makes
  the algorithms easy to read.

## Limitations

* HLL error is probabilistic (~1.6% standard error at p=12). Single queries can
  be off by 3% or more (max observed: 3.08%). Distinct counts across endpoints
  merge correctly, but intersections ("users of both A and B") are not
  supported.
* DDSketch accepts only non-negative values. Below 1e-9 they go to a zero bucket.
  If the low-end collapse ever triggers, low quantiles lose the guarantee (the
  sketch records a `collapsed` flag, but queries don't report it yet).
* No out-of-order *corrections*: you can add events but not retract them.
  Deletes or fixes need a rebuild of the affected cells from raw data.
* Query time is linear in the number of cells merged. That is fine for days to
  months of hourly data. Longer ranges would want monthly compaction (the same
  merge code).
* Batch-at-a-time ingestion in one process. There is no concurrency control
  beyond SQLite's transaction locking.
* Synthetic data only. The size of the naive baseline's error depends on how
  often users return and how heavy the tails are.
