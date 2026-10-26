# metricsql: a fan-out-safe semantic layer that compiles metrics to SQL

Ask three analysts for "revenue, orders and refunds by region" and you may get three numbers.
The usual bug is the **fan-out join**. An order has many items and a few refunds. If you join
`orders → order_items → refunds` and then `SUM(shipping_fee)` or `COUNT(order_id)`, every order
is counted once per item × refund row. The query runs, the numbers look plausible, and they are
wrong.

`metricsql` is a small semantic layer, in the spirit of dbt MetricFlow / LookML / Cube.
Metrics and dimensions are defined once in a JSON model. Queries name metrics and dimensions
only, and the compiler writes one SQLite statement that:

* aggregates each measure at **its own grain**, joining only along many-to-one edges, so the
  joins can't duplicate rows;
* **refuses** dimensions that would need a one-to-many join (for example orders by product
  category), instead of returning an inflated number;
* lines up measures from different tables on a shared `metric_time`, where each table uses its
  own date column (refunds by `refund_date`, orders by `order_date`);
* supports metric-level filters, ratio and derived metrics, time grains, and bound-parameter
  filters.

Python 3.10+ standard library and SQLite only (pytest for tests). All data is synthetic.

## Results (measured)

`python benchmark.py` checks every `(group, metric)` cell against an independent pure-Python
oracle (`metricsql/reference.py`: plain loops over raw rows, no SQL joins). A cell counts as
wrong if its relative error is above 1e-6. **naive** is the hand-written "join everything the
query touches, then GROUP BY" query (`metricsql/naive.py`). It uses the same metric definitions
and a correct `COUNT(DISTINCT)`, so its only mistake is the join.

Seed 7, 3,000 orders / 8,988 items / 361 refunds:

| query | method | cells | wrong | worst relative error |
|---|---|---|---|---|
| revenue, orders, aov by region | metricsql | 15 | **0** | 0 |
| | naive | 15 | 10 | 2.10 (210 % too high) |
| revenue, shipping, refunds, net revenue by month | metricsql | 56 | **0** | 0 |
| | naive | 56 | 52 | 8.26 |
| buyers, orders, units by channel | metricsql | 9 | **0** | 0 |
| | naive | 9 | 3 | 2.01 |
| completed revenue, refund count, orders by region × month, channel filter | metricsql | 198 | **0** | 0 |
| | naive | 198 | 116 | 11.0 |

With seed 2026 and 20,000 orders (`python benchmark.py --seed 2026 --orders 20000`), metricsql again
gets 0 wrong cells, and naive gets 10/15, 52/56, 3/9 and 128/207 wrong. Runtime for both is
26–125 ms per query on that data. The naive query's "monthly refunds" are also bucketed by order
date instead of refund date, which is a second error that comes from the same "one wide table"
approach.

Grand totals on seed 7 (`test_naive_baseline_overcounts_on_the_same_query`): correct
`orders = 3000`, while naive reports 9,185.

## Architecture

```
 samples/model.json ──► SemanticModel (validate: names, joins, metric DAG has no cycles,
                         derived-expression grammar, time_dimension reachable)
 query JSON / CLI ────► Query ──► Compiler
                                    1. expand metrics → leaf simple metrics
                                    2. group leaves by (dataset, metric filter) → one CTE each
                                    3. per CTE: BFS over many-to-one joins from that dataset to
                                       every dimension / filter dataset (unreachable → JoinPathError,
                                       two shortest paths → ambiguous)
                                    4. spine = UNION of group keys; LEFT JOIN each CTE with `IS`
                                    5. ratio / derived metrics on top of aggregated leaves
                                  ──► one SQL statement + bound params ──► SQLite (read-only)
```

Generated SQL for `samples/queries/category_completed.json` is in
`samples/expected_category_completed.sql`. Here is the shape for revenue + orders + refunds by
region:

```sql
WITH m0 AS (SELECT region, SUM(qty*price) AS revenue FROM order_items
            LEFT JOIN orders ... LEFT JOIN customers ... GROUP BY 1),
     m1 AS (SELECT region, COUNT(order_id) AS orders FROM orders LEFT JOIN customers ... GROUP BY 1),
     m2 AS (SELECT region, SUM(amount) AS refunds FROM refunds
            LEFT JOIN orders ... LEFT JOIN customers ... GROUP BY 1),
     spine AS (SELECT region FROM m0 UNION SELECT region FROM m1 UNION SELECT region FROM m2)
SELECT spine.region, COALESCE(m0.revenue,0), COALESCE(m1.orders,0), COALESCE(m2.refunds,0)
FROM spine LEFT JOIN m0 ON m0.region IS spine.region LEFT JOIN m1 ... LEFT JOIN m2 ...
```

| module | role |
|---|---|
| `metricsql/model.py` | Load and validate the JSON model: datasets, dimensions, measures, many-to-one joins, simple / ratio / derived metrics |
| `metricsql/compiler.py` | Dimension references, time grains, join-path search, CTE planning, final SQL |
| `metricsql/naive.py` | The join-everything baseline, kept to measure the error |
| `metricsql/reference.py` | Pure-Python oracle for the sample model, used by tests and the benchmark |
| `metricsql/synth.py` | Deterministic synthetic shop: customers, products, orders, order_items, refunds |
| `metricsql/cli.py` | `demo-db`, `list`, `query`, `run` |
| `benchmark.py` | Cell-level accuracy and timing, metricsql vs naive |

### Model format

```json
"orders": {
  "table": "orders", "primary_key": "order_id",
  "time_dimension": "orders__order_date",
  "joins": [{"to": "customers", "foreign_key": "customer_id"}],
  "dimensions": {"status": {"column": "status"},
                 "order_date": {"column": "order_date", "type": "time"}},
  "measures": {"order_count": {"agg": "count", "column": "order_id"}}
}
...
"metrics": {
  "revenue": {"type": "simple", "measure": "order_items.gross_revenue"},
  "completed_revenue": {"type": "simple", "measure": "order_items.gross_revenue",
                        "filter": [{"dimension": "orders__status", "op": "=", "value": "completed"}]},
  "aov": {"type": "ratio", "numerator": "revenue", "denominator": "orders"},
  "net_revenue": {"type": "derived", "expr": "revenue + shipping_revenue - refunds"}
}
```

* A `join` always declares a **many-to-one** edge, from the dataset holding the foreign key to
  the dataset with that primary key. That direction is what lets the compiler prove a join is
  safe.
* Dimension references are `dataset__dimension`, `dataset__time_dim__<grain>` or
  `metric_time__<grain>`. Grains are `day`, `week` (Monday start), `month`, `quarter` and `year`.
* `sql` fragments use `${TABLE}` for the dataset alias. They are written by the model author
  (trusted). Query input is only ever resolved by name or bound as a parameter.

## Setup

```bash
cd projects/2026-10-26-metricsql
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt     # only pytest; the package is stdlib-only
```

## Usage

```bash
python -m metricsql demo-db --out shop.db                       # seed 7: 3,000 orders
python -m metricsql list --model samples/model.json             # metrics + dimension references

python -m metricsql query --model samples/model.json --db shop.db \
    --metrics revenue,orders,aov,refund_rate --by customers__region --order=-revenue
python -m metricsql query --model samples/model.json --db shop.db \
    --metrics revenue,units --by products__category,metric_time__quarter \
    --where "orders__channel in web|app" --where "customers__region is not null" --explain

# saved queries (expected outputs are in samples/)
python -m metricsql run --model samples/model.json --db shop.db --format csv samples/queries/revenue_by_region.json
python -m metricsql run --model samples/model.json --sql samples/queries/category_completed.json   # SQL only
python -m metricsql run --model samples/model.json --db shop.db samples/queries/fanout_rejected.json
#   error: dataset 'products' is not reachable from 'orders' through many-to-one joins; ... (fan-out)

# the same question, answered the naive way
python -m metricsql query --model samples/model.json --db shop.db --metrics orders,refunds --naive

python benchmark.py
python -m pytest -q
```

`--where` accepts `=`, `!=`, `<`, `<=`, `>`, `>=`, `in a|b`, `not in a|b`, `is null` and
`is not null`. Write a descending `--order` as `--order=-revenue` so argparse doesn't read it as a
flag. Output formats are `table`, `csv` and `json`. The database is opened read-only.

| sample | expected output |
|---|---|
| `samples/queries/revenue_by_region.json` | `samples/expected_revenue_by_region.csv` (includes a `NULL` region group for customers with unknown region) |
| `samples/queries/monthly_net_revenue.json` | `samples/expected_monthly_net_revenue.csv` (Jan 2026 has refunds but no revenue, so net revenue is negative) |
| `samples/queries/category_completed.json` | `samples/expected_category_completed.csv`, `.sql` |
| `samples/queries/fanout_rejected.json` | `samples/expected_fanout_rejected.txt` (exit code 2) |

The expected files come from `demo-db` with the default seed 7, and the tests regenerate and
compare them.

## Tests

`python -m pytest -q` runs 61 tests:

* compiled results equal the oracle for 10 metrics across 5 group-by combinations, plus a
  query-level filter that applies to every dataset;
* fan-out rejection for group-bys, filters and derived metrics, and ambiguous (diamond) join
  paths;
* the naive baseline overcounting on the same query;
* metric semantics: divide by zero gives NULL, empty sums give 0 while empty max gives NULL,
  derived metrics use real division, metric filters stay local, `NULL` is its own group,
  refunds use their own time dimension;
* time-grain truncation (including Sunday → previous Monday);
* SQL-injection attempts in filter values stay bound parameters, and the table survives;
* 10 invalid queries and 10 invalid models (cycles, injected derived expressions, unknown
  references, bad aggregations, `__` in names, an unreachable time dimension);
* CLI: sample queries reproduce their expected files, `--where` parsing, read-only DB, refusing
  to overwrite a DB.

## Design decisions

* **Aggregate first, join second.** Each measure is aggregated in its own CTE at its own grain,
  then the CTEs are merged on the group-by keys. This is the standard fix for fan-out (the
  "symmetric aggregates" problem) and needs no `DISTINCT` tricks, which would be wrong for sums
  of equal values.
* **Reject instead of guessing.** "Orders by product category" has no single right answer (an
  order with a book and a toy: does it count in both categories, or split?). The compiler
  raises `JoinPathError` and names the edge, so a modeller has to decide. Ambiguous join paths
  fail the same way.
* **`metric_time` per dataset.** Each dataset declares which date it is measured on, possibly
  through a join (items use the order date). That makes "revenue minus refunds by month"
  well-defined, and it's the usual way cash-basis refunds are reported.
* **Union spine + `IS` joins** rather than `FULL OUTER JOIN` chains. A group that exists for
  only one measure (Jan 2026: refunds only) still appears, and `NULL` dimension values match
  each other.
* **Additive measures default to 0, non-additive ones to NULL.** A region with no refunds has
  0 refunds, but its average unit price is unknown, not 0.
* **Derived expressions use a whitelist grammar** (metric names, numbers, `+ - * / ( )`), so a
  model can't smuggle SQL into the outer query. Metric references are cast to REAL to avoid
  SQLite integer division.
* **A pure-Python oracle** instead of comparing SQL to SQL. The reference shares no code with
  the compiler, so a shared misconception can't make both agree.

## Limitations

* SQLite dialect only. Time grains use `strftime`/`date` on ISO-8601 text dates. There are no
  time zones, and weeks always start on Monday.
* Joins are many-to-one only. There are no role-playing dimensions (the same dataset joined
  twice, e.g. ship-to vs bill-to customer) and no many-to-many bridge tables. Those queries are
  rejected rather than supported.
* No cumulative / window metrics (running totals, trailing 7 days), period-over-period,
  `HAVING`-style metric filters, or caching / pre-aggregation.
* Filters are combined with AND only, and a query-level filter must be reachable from every
  dataset the query uses.
* Join cardinality is trusted from the model, not verified against the data. A wrong
  many-to-one declaration (duplicate keys on the "one" side) would still fan out.
* The oracle only covers the sample model's metrics and three group-bys. Other model shapes
  are covered by targeted unit tests, not by an oracle.
