# minidbt

A small dbt-style SQL model runner on SQLite (Python stdlib only; pytest for tests).

## Problem
Analytics SQL tends to live as ad-hoc scripts with implicit ordering and no checks. minidbt shows the core ideas of a transformation framework: models as `.sql` files, `ref()`-derived dependency graph, incremental materialization, selectors, failure propagation, and data tests.

## Architecture
- `minidbt/project.py` – loads `models/*.sql`, `seeds/*.csv`, `tests.toml`; parses `config()`, `ref()`, `{{ this }}`, `{% if is_incremental %}`; deterministic Kahn toposort with cycle detection; selectors (`m`, `+m`, `m+`).
- `minidbt/runner.py` – loads seeds, materializes models as `view`, `table` or `incremental` (delete-by-`unique_key` then insert from a temp delta), skips models whose upstream failed, runs data tests (`not_null`, `unique`, `accepted_values`, `relationships`, singular `sql` tests that return offending rows).
- `minidbt/cli.py` – `run | test | build | lineage`.
- `example/` – seeds, four models (staging views, a table, an incremental model), tests, and `expected_output.txt` / `expected_lineage.txt`.

## Setup & usage
```
pip install -r requirements.txt     # only pytest, for tests
python -m minidbt build --project example               # seeds + models + tests (in-memory DB)
python -m minidbt run --project example --db wh.sqlite --select +daily_revenue
python -m minidbt test --project example --db wh.sqlite
python -m minidbt lineage --project example
python -m pytest -q
```
Exit codes: 0 ok, 1 model error/skip or failed test, 2 project error (cycle, unknown ref, bad config).

## Design decisions
- Dependencies come only from `ref()`, so the graph can't drift from the SQL.
- Incremental runs re-process from `MAX(key column)` forward and merge by `unique_key`, so late rows for the latest day are corrected rather than duplicated; a test checks that incremental + full-refresh results are identical.
- SQLite views are lazily validated, so views are queried with `LIMIT 0` after creation to make broken SQL fail at the right model.
- Identifiers used in generated test SQL are whitelisted (`[A-Za-z0-9_]`); `tests.toml` is trusted input for singular SQL tests.
- Tests run only after a clean build in `build`.

## Limitations
- Incremental logic cannot see rows older than the max key (late data for older days needs `--full-refresh`).
- `config()` accepts only single-quoted string values; no Jinja beyond the three constructs above; no macros, docs, snapshots, or column-level lineage.
- Seeds infer int/float/text per value, not per column. SQLite only.

## Test results
`python -m pytest -q`: 11 passed (run on 2026-10-22, Python 3.11 / pytest).
