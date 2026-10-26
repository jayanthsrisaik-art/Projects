# cdcsync: CDC stream applier that converges despite reordering, duplicates and crashes

Change-data-capture (CDC) pipelines rarely deliver changes the way the source made them.
Partitions get rebalanced, consumers retry, the initial snapshot overlaps the live stream,
and a consumer can crash halfway through a batch. A consumer that just "upserts every message"
can bring back deleted rows, overwrite new values with old ones, and expose half-applied
transactions.

`cdcsync` applies a Debezium-style JSONL change stream to a SQLite replica. The replica ends
up in the same state as the source whatever the delivery order, and a reconciler proves it by
comparing bucketed checksums and naming the exact keys and columns that differ.

Python 3.10+ standard library only (pytest for tests). All data is synthetic.

## What it handles

| Problem | Mechanism |
|---|---|
| Out-of-order delivery / snapshot overlapping the stream | Each row stores `_cdc_lsn`. A change applies only if its LSN is **strictly greater** (last-writer-wins by LSN). |
| Delete arrives before an older create/update | Deletes leave a **tombstone** (`_cdc_deleted = 1`, with its LSN), so older images can't bring the row back. A re-insert with a higher LSN still works. |
| Transactions split across the stream, `tx_end` arriving before its changes | Changes are **staged** in `_cdc_staging`. The tx applies atomically once `tx_end` has arrived and exactly `event_count` distinct changes are staged. |
| Duplicates (at-least-once delivery) | Staging is keyed `(tx, lsn)`. Changes and markers for already-committed txs are dropped via `_cdc_applied_tx`. Non-tx duplicates are no-ops thanks to the LSN guard. |
| Consumer crash mid-batch | Each batch is one SQLite transaction holding the data writes, the staged changes **and** the per-source line offset. A crash rolls everything back, and the next run resumes at the committed offset. |
| Malformed records / SQL-injection-shaped names | Records are validated (identifiers, scalar values, LSN type, key/after consistency) and **quarantined** in `_cdc_rejects` with a reason and line number. They don't stop the pipeline. |
| Additive schema change (new column) | `ALTER TABLE ADD COLUMN` on first sight, then the `v_<table>` view is rebuilt. Older row images leave the new column NULL. |
| "Is the replica actually right?" | `reconcile` hashes rows into buckets by primary key, compares bucket digests, then diffs only the mismatched buckets: missing keys, extra keys, and which columns differ. |

## Architecture

```
            ┌──────────────┐  events.jsonl (reordered, duplicated, junk)
 simulator ─┤ SourceSim    ├──────────────► chaos() ───────────────┐
            │ (snapshot +  │                                        ▼
            │  300 txs)    │──► source.db                 ┌──────────────────┐
            └──────────────┘        │                     │ Applier          │
                                    │                     │  parse/validate  │──► _cdc_rejects
                                    │                     │  stage tx  ──────│──► _cdc_staging/_cdc_tx_end
                                    │                     │  LSN-guarded     │
                                    │                     │  upsert/tombstone│──► customers, orders (+ v_*)
                                    │                     │  offset per batch│──► _cdc_offsets
                                    │                     └──────────────────┘
                                    │                              │ target.db
                                    └────────► reconcile ◄─────────┘
                                         bucket digests → row diffs
```

* `cdcsync/events.py`: record format, validation, `RejectError`.
* `cdcsync/applier.py`: `Applier` (the real thing) and `NaiveApplier` (arrival-order baseline).
* `cdcsync/simulator.py`: deterministic source simulator plus `chaos()` (bounded reordering, duplicates, junk lines).
* `cdcsync/reconcile.py`: bucketed checksum reconciliation.
* `cdcsync/cli.py`: `simulate`, `apply`, `status`, `dump`, `reconcile`.
* `benchmark.py`: measured comparison against the naive baseline.

### Record format

```json
{"type": "change", "table": "orders", "op": "u", "key": {"id": 10},
 "after": {"id": 10, "customer_id": 1, "amount_cents": 2500, "status": "paid"},
 "lsn": 120, "tx": "t2"}
{"type": "tx_end", "tx": "t2", "event_count": 1}
```

`op` is `c`/`u`/`d`/`r` (snapshot read). `after` is a full row image (`null` for deletes).
`tx` is `null` for snapshot reads and autocommit changes.

## Setup

```bash
cd projects/2026-10-26-cdcsync
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # only pytest; the package itself is stdlib-only
```

## Usage

```bash
# 1. Synthetic source + chaotic stream (snapshot, 300 txs, schema change midway)
python -m cdcsync simulate --out out --seed 7 --window 20 --dup-rate 0.05
# 2. Apply (re-running is a no-op; the offset is committed with the data)
python -m cdcsync apply out/events.jsonl --target out/target.db --batch-size 100
# 3. Check: exit code 0 when the replica equals the source, 1 otherwise
python -m cdcsync reconcile --source out/source.db --target out/target.db
# Inspect offsets, pending transactions, rejects, tombstones
python -m cdcsync status --target out/target.db
python -m cdcsync dump --target out/target.db
# Baseline for comparison
python -m cdcsync apply out/events.jsonl --target out/naive.db --naive
```

Output of step 2 for seed 7 (measured):

```json
{"applied": 828, "duplicates": 52, "lines_read": 1192, "pending_tx": 0,
 "rejected": 3, "stale_skipped": 9, "tx_committed": 300}
```

The reconcile for that run reports `"ok": true`: customers 114/114 rows, orders 179/179, 0 of
16 buckets mismatched.

### Sample input and expected output

`samples/events.jsonl` is a hand-written 15-line stream that exercises each edge case:

* `tx_end` arrives before its changes.
* A late snapshot read.
* A duplicate change from an already-committed tx.
* A delete followed by an *older* update (it must not resurrect the row).
* A new column (`loyalty_tier`).
* A truncated JSON line.
* An incomplete transaction (`t5`, with no `tx_end`).
* An autocommit insert.

```bash
python -m cdcsync apply samples/events.jsonl --target out/sample.db   # == samples/expected_stats.json
python -m cdcsync dump --target out/sample.db                         # == samples/expected_state.json
```

Expected: 7 applied, 1 stale skipped, 1 duplicate, 1 rejected, 4 txs committed, 1 pending.
Order 10 stays deleted, and order 11 is not visible because `t5` never completed.

## Tests

```bash
python -m pytest -q
```

37 tests cover the following:

* Validation of 13 malformed record shapes.
* Exact sample outputs, through both the API and the CLI.
* Stale-update skipping, tombstones against early deletes, and re-insert after delete.
* Transactional atomicity, with incomplete and over-full transactions held back.
* Late duplicates of committed transactions.
* Schema evolution, rejection of a primary-key change, and composite keys.
* Idempotent re-runs, including a replay under a new source name.
* Four simulated crashes at different points (including batch boundaries), then a resume that gives the same state as a clean run.
* Convergence on 5 seeds with a reorder window of 60 and 10% duplicates.
* Convergence under a fully random permutation of the stream.
* The naive baseline diverging on the same input.
* The reconciler pinpointing a tampered column and a hidden row.
* Deterministic, bounded chaos.

Mutation checks were run while developing (they are not part of the suite):

* Disabling the LSN guard fails 12 tests.
* Removing the over-full transaction check fails 1 test.

## Measured comparison

`python benchmark.py` (5 seeds × 300 transactions, about 1.1k events per stream, 5% duplicates,
3 junk lines). "Divergent rows" counts missing + extra + value-mismatched rows after reconciliation.

| reorder window | dup rate | runs | naive: runs diverged | naive: divergent rows (mean) | cdcsync: runs diverged | cdcsync: events/s (median) |
|---|---|---|---|---|---|---|
| 0 | 0.05 | 5 | 0 | 0.0 | 0 | 21,362 |
| 5 | 0.05 | 5 | 0 | 0.0 | 0 | 23,459 |
| 20 | 0.05 | 5 | 5 | 2.6 | 0 | 16,143 |
| 100 | 0.05 | 5 | 5 | 10.6 | 0 | 27,385 |

How to read this:

* With little reordering, duplicates of nearby events are harmless even for the naive consumer.
* Once events move about 20 positions, every naive run drifts from the source.
* cdcsync matched the source in all 20 runs.
* Throughput numbers come from one container on short runs. They are noisy and only show the order of magnitude.

## Design decisions

* **LSN-based last-writer-wins instead of reordering.** Buffering everything until it can be
  sorted needs a bound on lateness and unbounded memory. Comparing per-row LSNs converges for
  *any* delivery order, as long as every event eventually arrives (and the permutation test
  checks exactly that).
* **Full row images, not diffs.** With a full `after` image, applying the max-LSN change is
  enough. Partial updates would need ordered application per key.
* **Tombstones kept, not purged.** They are what makes delete-then-late-update safe. Purging
  is safe only after a lateness horizon, which this tool doesn't assume (see limitations).
* **Offset and data in one SQLite transaction.** This gives exactly-once *effect* without a
  separate checkpoint store. Re-delivery is cheap anyway because every step is idempotent.
* **Staging persisted in the target.** Incomplete transactions survive restarts and are visible
  in `status`, instead of being lost in consumer memory.
* **Over-full transactions are held, not applied.** If more distinct changes arrive than
  `event_count` announces, the producer and consumer disagree, and applying a guess would be
  silent corruption.
* **Untyped SQLite columns.** Values are stored exactly as delivered (dynamic typing), so
  reconciliation compares like with like and no type mapping is needed.
* **Identifiers validated and quoted; values always bound as parameters.** The injection-shaped
  table name in the synthetic stream lands in `_cdc_rejects`.

## Limitations

* Per-row consistency is guaranteed, but cross-transaction ordering is not. While events are
  still in flight, the replica can show tx 12's effects before tx 11's. It converges once the
  stream is drained.
* Only additive schema changes: no column drops, renames or type changes. A primary-key change
  is rejected.
* Tombstones are never compacted, and a transaction whose `tx_end` never arrives stays in
  staging until someone deals with it (it shows up in `status`).
* LSNs must be totally ordered across the source. This is true for a Postgres WAL or a MySQL
  binlog position, but not for a multi-primary source.
* Single-writer SQLite target. Offsets are per input file name (or `--source-name`), not per
  Kafka partition.
* The reconciler loads both sides into memory. At scale, the bucket digests would be computed
  inside each database.
* The input is a Debezium-*style* format, not the exact Debezium envelope. Mapping the real
  envelope (`payload.op`, `payload.source.lsn`, transaction topic) would be a thin adapter.
