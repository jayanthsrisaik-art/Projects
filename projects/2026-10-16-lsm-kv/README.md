# lsmkv – a small LSM-tree key-value store

## Problem
Understand and demonstrate how write-optimised storage engines (LevelDB/RocksDB style) work: durable writes, sorted immutable files, tombstone deletes, compaction and crash recovery – in ~350 lines of dependency-free Python.

## Architecture
```
put/delete ─► WAL (crc32 per record, append) ─► memtable (dict)
                                   │ size ≥ limit
                                   ▼
                         flush → SSTable NNNNNNNN.sst (sorted, sparse index, bloom filter, crc32 footer)
                                   │ #tables ≥ compact_at
                                   ▼
                    full compaction → single SSTable (shadowed versions + tombstones dropped)
get: memtable → SSTables newest→oldest (bloom filter skips files)      scan: k-way heap merge, newest wins
```
- `wal.py` – append-only log; replay stops at a torn/corrupt tail.
- `sstable.py` – immutable file, written to `.tmp` then atomically renamed; whole-file CRC checked on open.
- `bloom.py` – double-hashing Bloom filter (1% target false-positive rate).
- `store.py` – `LSMStore` (`put`, `get`, `delete`, `scan`, `flush`, `compact`, `close`).
- `cli.py` – runs a command script.

## Setup & usage
```
pip install -r requirements.txt     # pytest only; the library is stdlib-only (Python ≥3.10)
python -m pytest -q
PYTHONPATH=src python -m lsmkv.cli /tmp/db run samples/script.txt   # output matches samples/expected_output.txt
```
```python
from lsmkv import LSMStore
with LSMStore("/tmp/db") as db:
    db.put(b"k", b"v"); db.delete(b"x"); list(db.scan(b"a", b"z"))
```

## Testing
12 tests, including a randomized 3000-op comparison against a dict model with periodic close/reopen, WAL torn-write recovery, tombstone shadowing across SSTables, checksum corruption detection, Bloom filter false-negative/false-positive check, and the CLI sample.

## Design decisions
- Tombstones are only dropped during *full* compaction, which is the only compaction, so this is always safe.
- Compaction is a single-step full merge (simple to reason about) rather than leveled/tiered.
- Memtable is a dict sorted on flush (simple; a skiplist would give ordered iteration without sorting).

## Limitations
- Single process, single thread; no locking. Not safe for concurrent writers.
- SSTables are loaded fully into memory; full compaction is O(total data) and not incremental.
- Flush/compaction crash windows: a crash between writing a compacted table and deleting old ones leaves duplicate-but-consistent data (newer table wins by sequence); no manifest file.
- WAL fsync is off by default (`sync=True` to enable), so power loss can drop recent writes. No benchmarks are claimed.
