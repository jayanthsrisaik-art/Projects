# scd2-warehouse

Build a **Type-2 slowly changing dimension** (full change history with validity
ranges) from plain daily CSV snapshots, using one set-based SQL statement on
SQLite. Stdlib only; `pytest` is the sole dependency.

## Problem
Source systems often expose only "today's table". To answer *"what tier was
customer 1 on Jan 2?"* you need history. Most hand-rolled SCD2 loaders are
incremental and break on re-runs, deletes, re-appearing keys and late-arriving
files. This project treats raw snapshots as the source of truth and derives the
dimension as a pure function of them.

## Architecture
```
CSV snapshot --load--> stg_snapshot (+ row hash) --rebuild (SQL)--> dim_scd2
                       snapshot_index (date, rows, content hash)
```
* `stg_snapshot`: every snapshot row, keyed by `(as_of, key)`, with a hash of the tracked attributes.
* `dim_scd2`: `sk, <key>, <attrs>, valid_from, valid_to, is_current`. Ranges are half-open `[valid_from, valid_to)`; `valid_to` is NULL for the open version.
* Rebuild uses gaps-and-islands: `LAG` flags where a key's hash changes or the key is missing from the previous snapshot; a running `SUM` numbers the runs; each run becomes a version whose `valid_to` is the date of the first snapshot after the run (the key's change/deletion date).
* `check` verifies invariants: no overlaps, ≤1 open version per key, current rows == latest snapshot, and that **reconstructing every historical snapshot from the dimension reproduces it exactly**.

## Setup
```
python -m pip install -r requirements.txt   # Python 3.10+ (SQLite window functions)
python -m pytest -q
```

## Usage
```
python -m scd2 --db wh.db load samples/snapshots/customers_2026-01-01.csv --as-of 2026-01-01 --key customer_id
python -m scd2 --db wh.db check
python -m scd2 --db wh.db as-of 2026-01-02     # state on a date (CSV)
python -m scd2 --db wh.db history 1            # all versions of key 1
python -m scd2 --db wh.db snapshots
sh samples/run_demo.sh                         # full demo; output in samples/expected/demo_output.txt
```
The demo loads Jan 4 *before* Jan 3 to show late arrivals give the same result as in-order loading.
Exit codes: 0 ok, 1 `check` violations, 2 bad input.

## Design decisions
* **Rebuild over incremental merge**: idempotent, order-independent and trivially correct; cost is O(total history) per load.
* **Deletes are versions ending**, and a key that reappears starts a new version (so history shows the gap).
* Re-loading identical content is a no-op; different content for an existing date is rejected unless `--replace`.
* Schema (key + attribute columns) is fixed by the first load; column names are validated against an identifier whitelist since they are interpolated into SQL.
* A randomized test compares the SQL output against a naive Python replay over 25 seeded histories with shuffled arrival order.

## Limitations
* Values are stored as text; no type coercion, empty string ≠ NULL.
* Full rebuild each load: fine for 10^5–10^6 rows, not for very large dimensions.
* Dates are snapshot dates, not event times: a change is dated to the first snapshot where it is seen.
* No schema evolution, single-column business key, SQLite only.
