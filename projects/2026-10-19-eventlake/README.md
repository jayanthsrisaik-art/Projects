# eventlake

Contract-validated, idempotent ingestion of JSONL event files into a date-partitioned, gzip-compressed mini data lake. Pure Python standard library (pytest for tests only).

## Problem
Event feeds arrive as append-only files with duplicates, replays, malformed lines, schema drift and late data. A pipeline needs to load them repeatedly without double-counting, never silently drop bad rows, and survive a crash mid-run.

## Architecture
```
 *.jsonl --> checkpoint (skip processed lines)
         --> JSON parse        --fail--> quarantine (malformed_json)
         --> contract validate --fail--> quarantine (per-field reasons)
         --> unknown fields: counted as drift, dropped
         --> dedupe on key (state db + in-batch) --> counted duplicate
         --> lateness check (event_time < max_seen - allowed_lateness) --> quarantine (late_arrival)
         --> lake/<dataset>/dt=YYYY-MM-DD/part-<src>-<from>-<to>.jsonl.gz   (atomic temp+rename)
         --> commit state (checkpoint, seen keys, max event time) in one SQLite transaction
```
- `eventlake/contract.py` – JSON contract (types `string,int,float,bool,timestamp`; required, enum, min/max); timestamps must carry a timezone and are partitioned by UTC date.
- `eventlake/ingest.py` – ingestion, quarantine, `compact`, `read_partition`.
- `eventlake/state.py` – SQLite state. `eventlake/cli.py` – CLI.

## Setup & usage
```
pip install -r requirements.txt     # only needed for tests
python -m eventlake ingest --contract samples/orders.contract.json --lake out/lake \
    samples/orders_day1.jsonl samples/orders_day2.jsonl     # prints a JSON report
python -m eventlake compact --lake out/lake --dataset orders
python -m eventlake query --lake out/lake --dataset orders --partition dt=2026-03-02
python -m pytest -q
```
`samples/expected_day1_report.json` is the report for ingesting `orders_day1.jsonl` alone (checked by a test). Ingesting both sample files in one run gives read 12, accepted 5, duplicates 2, late 1, invalid 3, malformed 1.

## Design decisions
- **Idempotency by construction**: batch file names derive from (source, line range), so a crash after writing files but before the state commit is repaired by re-running – the same files are rewritten byte-identically (gzip mtime fixed). Tested by injecting a commit failure.
- **Duplicates are checked before lateness**, so replays of old events are reported as duplicates rather than late.
- **Quarantine, don't drop**: every rejected line is stored with source, line number and reasons.
- Compaction rewrites a partition's batch files into one `compact.jsonl.gz`; dedupe state is independent of file layout.

## Limitations
- Checkpoints are line counts: files must be append-only (rewritten/rotated-in-place files are not detected).
- "Late" is relative to the max event time seen, and is evaluated in file-sorted order, so results depend on ingestion order.
- The `seen` key table grows unbounded (no TTL); single-writer only (no locking across processes).
- Compaction is not crash-atomic across several files (a crash mid-unlink leaves compact + parts, which could double-read). Gzip JSONL stands in for Parquet because no binary dependencies are required.
- Tested on Python 3.13 only; no performance benchmarks were run.
