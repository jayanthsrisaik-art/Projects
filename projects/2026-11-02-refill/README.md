# refill: partition-level backfill planner

When a source partition is corrected (late-arriving rows, a bad refund load, a
dimension fix), which downstream partitions have to be recomputed?
Teams usually guess. Some rebuild everything, some rebuild "from the earliest
changed date onward", and some rebuild only "the same dates". The first is
slow and the other two can leave **wrong numbers in the warehouse**.

`refill` makes the answer exact. Every derived table declares *how* each of its
partitions depends on upstream partitions (`same`, `window(n)`, `lag(n)`,
`rollup`, `month_of`, `all`). The planner walks those mappings backwards from
the changed partitions to get the **minimal invalidation closure**, puts it in
dependency order with parallel waves, and explains *why* each partition is in the
plan. A SQLite executor runs the plan as idempotent partition overwrites. It can
also use **early cutoff**: when a recomputed partition's content digest does not
change, propagation stops there.

Everything is checked against a from-scratch rebuild oracle.

## Architecture

```
pipeline.json + sql/*.sql
        │  spec.py: load, validate grains/mappings, SQL-reference lint, table DAG
        ▼
   Pipeline (Calendar + tables + downstream index)
        │
        │  --change raw_orders:2026-10-05..2026-10-06
        ▼
 planner.plan()          BFS over mapping.inverse()  ──►  tasks, reasons, waves
 planner.brute_force_affected()  forward-closure oracle (used in tests)
        │
        ▼
 executor.run_tasks()    DELETE part + INSERT…SELECT per partition (one txn each)
                         optional early cutoff via per-partition SHA-256 digests
        │
        ▼
 demo.run_demo()         every strategy vs. fresh full rebuild, partition by partition
```

| module | responsibility |
|---|---|
| `refill/partitions.py` | `Calendar` (day/month/none partitions, clipping) and `Mapping` with `forward` / `inverse` |
| `refill/spec.py` | spec loading, validation, table topological order, `parse_change` |
| `refill/planner.py` | `plan`, `explain`, brute-force oracle, baseline strategies |
| `refill/executor.py` | SQLite schema, partition overwrite, digests, cutoff execution |
| `refill/synth.py` | seeded synthetic orders/refunds/customers and four correction scenarios |
| `refill/demo.py`, `refill/cli.py` | measurement harness and CLI |

### Dependency mappings

| mapping | grains (out ← in) | out partition reads | a changed input invalidates |
|---|---|---|---|
| `same` | any equal | the same key | the same key |
| `window(n)` | day ← day | `d-n+1 … d` | `d … d+n-1` |
| `lag(n)` | day ← day | `d-n` | `d+n` (self-deps must use this) |
| `rollup` | month ← day | every day of the month | its month |
| `month_of` | day ← month | the day's month | **every day of that month** |
| `all` | any | every partition | every partition |

Every mapping is clipped to the pipeline range. A self-dependency (`cumulative_net` reads
yesterday's `cumulative_net`) is allowed only as `lag(n≥1)`, which keeps the
partition graph acyclic. A change then chains forward to the end of the range.

## The example pipeline

`examples/pipeline.json` has 3 sources (`raw_orders`, `raw_refunds` by day,
`dim_customer` unpartitioned) and 6 derived tables over 2026-08-01…2026-10-31,
for 463 derived partitions:

```
daily_region_sales   day    raw_orders[same], raw_refunds[same], dim_customer[all]
cumulative_net       day    daily_region_sales[same], cumulative_net[lag(1)]
dod_change           day    daily_region_sales[same], daily_region_sales[lag(1)]
monthly_region_sales month  daily_region_sales[rollup]
daily_share_of_month day    daily_region_sales[same], monthly_region_sales[month_of]
region_sales_7d      day    daily_region_sales[window(7)]
```

SQL lives in `examples/sql/`. The SQL can use `:p`, `:p_start`, `:p_end`, `:prev` and `:month`.

## Setup

```bash
cd projects/2026-11-02-refill
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt      # only pytest; the runtime is stdlib
```

## Usage

```bash
python -m refill lint examples/pipeline.json

python -m refill plan examples/pipeline.json \
    -c raw_orders:2026-10-05..2026-10-06 \
    -e daily_share_of_month:2026-10-02 -e region_sales_7d:2026-10-12
```

```
roots: 2 partition(s)
plan: 72 of 463 derived partitions, 28 wave(s)
  daily_region_sales        2  2026-10-05..2026-10-06
  cumulative_net           27  2026-10-05..2026-10-31
  dod_change                3  2026-10-05..2026-10-07
  monthly_region_sales      1  2026-10
  daily_share_of_month     31  2026-10-01..2026-10-31
  region_sales_7d           8  2026-10-05..2026-10-12
why daily_share_of_month:2026-10-02: daily_share_of_month:2026-10-02 <- monthly_region_sales:2026-10 <- daily_region_sales:2026-10-05 <- raw_orders:2026-10-05
why region_sales_7d:2026-10-12: region_sales_7d:2026-10-12 <- daily_region_sales:2026-10-06 <- raw_orders:2026-10-06
```

Change syntax: `table:YYYY-MM-DD`, `table:start..end`, `table:YYYY-MM` (month
tables), `table:*`. You can name a *derived* table to plan a logic change, e.g.
`-c monthly_region_sales:2026-09`. Add `--json` for machine-readable plans. The
CLI exits with code 2 on spec or change errors.

```bash
python -m refill demo examples/pipeline.json          # measured comparison
python -m refill demo examples/pipeline.json --json   # deterministic (no timings)
```

Sample inputs: `examples/pipeline.json`, `examples/sql/`, `examples/changes.txt`.
Expected outputs: `examples/expected/` (text plan, JSON plan, demo report). The
CLI tests compare against these files exactly.

## Measured results

From `python -m refill demo examples/pipeline.json` (seed 7, 463 derived
partitions, Python 3.11, SQLite 3.45). The *wrong* column counts partitions whose
content differs from a fresh full rebuild of the corrected sources. Timings come
from one run and are only meant for comparing strategies against each other.

| scenario | truly changed | full rebuild | from earliest date | same dates only | **refill** | **refill + cutoff** |
|---|---:|---|---|---|---|---|
| late orders (2 days) | 72 | 463 run, 0 wrong | 136 run, **4 wrong** | 11 run, **61 wrong** | 72 run, 0 wrong | 72 run, 0 wrong |
| refund fix (1 day) | 89 | 463 / 0 | 242 / **13 wrong** | 6 / **83 wrong** | 89 / 0 | 89 / 0 |
| customer region move (`dim:*`) | 232 | 463 / 0 | 463 / 0 | 463 / 0 | 463 / 0 | **316** / 0 |
| identical re-land (1 day) | 0 | 463 / 0 | 368 / 0 | 6 / 0 | 115 / 0 | **1** / 0 |

Wall time for the late-orders scenario: full rebuild 0.125 s, refill 0.005 s.

What the numbers show:

* **"From earliest date" is not safe.** `daily_share_of_month` reads the whole
  month total, so a change on Oct 5 changes Oct 1–4 too, and those days come
  *before* the changed date. The 4 and 13 wrong partitions are exactly those days.
* **"Same dates only"** misses the 7-day window, the day-over-day lag and the
  running total chain.
* **refill**'s static plan for the two-day late-orders correction is exactly the
  72 partitions that changed. It runs 72 tasks where a full rebuild runs 463.
* **Early cutoff** helps when the static plan has to be pessimistic. A dimension
  change has an `all` mapping, but only days where that customer had activity
  really change. A byte-identical re-land stops after one task.

## Tests

```bash
python -m pytest -q
```

The tests cover:

* `forward` and `inverse` are exact converses for every mapping over a calendar with partial months.
* The planner equals the brute-force forward-closure oracle on 60 random root sets.
* Waves respect dependencies, and every `explain` chain starts at a root.
* Static and cutoff execution are content-identical to a full rebuild across 24 random source edits.
* Overwrites are idempotent, cutoff stops on identical data, and derived logic roots are forced.
* Spec validation catches undeclared or unused SQL references, cycles, bad grains and non-lag self-deps.
* The baselines fail in the specific, documented ways.
* The CLI output matches the golden files.

## Design decisions

* **Inverse mappings for planning, forward mappings for checking.** Planning
  is driven by the change (cost scales with the plan, not the warehouse). An
  independent forward oracle plus a converse property test catch mapping bugs.
* **Mappings are declared, then linted against SQL.** The planner trusts the
  declared dependencies, so `lint` refuses SQL that reads an undeclared table or
  declares a table it never reads. This is the most common way partition
  lineage drifts from the code.
* **Partition overwrite, not merge.** Each task is `DELETE WHERE part=…` +
  `INSERT … SELECT` in one transaction, so re-running a plan is always safe.
* **(table topo index, key) ordering.** Cross-table deps point to earlier
  tables and self-deps point to earlier keys, so this ordering is a valid
  schedule. Waves (longest in-plan dependency chain) show how much of the
  plan could run in parallel.
* **Cutoff uses content digests, not row counts.** A recomputed partition
  marks its readers dirty only when its sorted-row SHA-256 changes.

## Limitations

* Correctness depends on the declared mappings matching what the SQL does.
  The lint only checks *which* tables are read, not *which partitions*. A
  `window(7)` declared over SQL that reads 8 days would go unnoticed.
* The only grains are day, month and unpartitioned, and there is one shared date range.
  There are no hourly or weekly grains, per-table ranges, or arbitrary partition functions.
* Execution is sequential on one SQLite connection. Waves are reported but not
  run in parallel.
* Cutoff has to read each partition before and after the rewrite. For huge
  partitions a stored digest (or engine-level checksum) would be cheaper.
* Source changes must be declared by the caller. refill does not detect which
  source partitions changed (pair it with a checksum/CDC step for that).
* All data is synthetic. Timings are in-memory SQLite on a small dataset.
