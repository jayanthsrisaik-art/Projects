# winstream: event-time windows that stay correct when data arrives late

Sensors, mobile apps and edge devices send events out of order. Retries and
slow links hold some readings back for minutes. A device that goes offline
uploads its backlog when it reconnects, and at-least-once delivery sends some
events twice. A consumer that buckets events by *arrival* time produces
per-minute numbers that are simply wrong. A consumer that buckets by *event*
time has to answer three questions:

1. **When is a window complete enough to report?** (the watermark)
2. **What happens to an event that arrives after its window was reported?**
   (allowed lateness: update the result, or drop the event and record why)
3. **What happens after a crash mid-stream?** (no lost and no doubled rows)

`winstream` is a small stream processor that answers each question explicitly
and then checks its output against an independent batch oracle:

* **Windows**: tumbling, hopping and session windows. Every key gets its own
  windows, and all keys share one global watermark.
* **Watermark**: `max event time seen − delay` (bounded out-of-orderness).
* **Allowed lateness**: a window stays open after it fires. A late event
  produces an `update` row with a higher `revision`, and an event past the
  horizon goes to a `late` side output.
* **Session merging with retractions**: a late event can bridge two sessions
  that were already reported. The job then emits `retract` rows for both and
  one `insert` for the merged session.
* **Idempotent against re-delivery**: each accumulator keeps the ids it has
  seen, so a duplicate event changes nothing.
* **Exactly-once SQLite sink**: changelog rows, the materialised `results`
  table, side outputs, operator state and the source offset are committed in
  **one transaction**. A crash at any point followed by a restart produces
  byte-identical output.
* **Verification**: `winstream verify` compares the job with a separately
  written batch GROUP BY. It also replays the changelog and reruns the job
  with injected crashes.
* **Benchmark**: a measured table of the latency and completeness each
  configuration trades, plus a processing-time baseline.

Python standard library only (sqlite3, heapq). No external services.

## Architecture

```
events.jsonl (arrival order, offset = line number)
      │  parse_event ── invalid ──────────────────────────────┐
      ▼                                                       │
 Operator (engine.py)                                         │
   assign(ts) → windows per key                               │
   drop rule vs current watermark ── late ──────────────────┐ │
   Acc.add (dedupe by id) ── duplicate ───────────────────┐ │ │
   session merge → retract old / keep identity → update   │ │ │
   wm = max_ts − delay                                    │ │ │
   fire-heap  (end ≤ wm)        → insert                  │ │ │
   purge-heap (end + L ≤ wm)    → state dropped           │ │ │
      │ emissions                                         ▼ ▼ ▼
      ▼                                             side outputs
 Sink.commit (sink.py), every N records, ONE SQLite transaction:
   changelog (seq, kind, key, window, revision, aggregates, wm, trigger)
   results   (upsert on insert/update, delete on retract)
   side_output
   checkpoint (next_offset, next_seq, config, operator state JSON)
```

| module | responsibility |
|---|---|
| `winstream/model.py` | `Event`, JSONL parsing/validation, `WindowSpec` assigners, mergeable `Acc` |
| `winstream/engine.py` | the operator: watermark, drop rules, firing, updates, session merge/retract, purge, state snapshot |
| `winstream/sink.py` | SQLite schema, atomic commit of output + state + offset, crash injection hook |
| `winstream/pipeline.py` | source loop, periodic checkpoints, resume, `run_until_done` with a crash schedule |
| `winstream/oracle.py` | independent batch reference: replays only the watermark formula, then a plain GROUP BY or sort-and-sweep sessioniser |
| `winstream/verify.py` | oracle comparison, changelog replay, premature-insert and sequence checks |
| `winstream/bench.py` | trade-off measurement and processing-time baseline |
| `winstream/generator.py` | synthetic disordered stream (stragglers, offline backlogs, re-deliveries) |
| `winstream/cli.py` | `generate`, `run`, `verify`, `show`, `bench` |

### Semantics

| | rule |
|---|---|
| watermark | `wm = max(ts) − delay`; never decreases; undefined before the first event; `+∞` at end of input (`flush`) |
| first result | `insert`, revision 0, when `wm ≥ window end` |
| late, kept | fixed windows: if `end + lateness > wm` the event is added and an `update` (revision + 1) is emitted when the window had already fired |
| late, dropped | fixed windows: an (event, window) pair with `end + lateness ≤ wm` goes to `side_output(reason='late')`. Hopping windows decide this per window. |
| sessions | an event is dropped only if `ts < wm − lateness` **and** it does not fall inside a session that is still held (`start ≤ ts ≤ last event`) |
| session merge | old emitted sessions are `retract`-ed. The merged session gets a new identity and is inserted when `wm ≥ its end` (at once if the watermark is already past it). An event that falls inside one session keeps that session's identity and produces an `update`. |
| purge | state is freed once `end + lateness ≤ wm` |
| duplicates | same id for the same window/session → ignored, `side_output(reason='duplicate')` |
| invalid rows | `side_output(reason='invalid', detail=...)`; the stream continues |

The session drop rule is chosen so that an accepted event can never overlap a
session that has already been purged. So the streaming result is *exactly*
the batch sessionisation of the accepted events, and that is what the oracle
checks.

## Setup

```bash
cd projects/2026-11-05-winstream
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt      # pytest only; runtime is stdlib
```

Requires Python 3.11+.

## Usage

```bash
# 1. a synthetic stream (bundled as samples/events.jsonl: 6 devices, 2 h, 1214 rows)
python -m winstream generate --out samples/events.jsonl --keys 6 --duration 7200

# 2. run a job into SQLite (resumable; checkpoints every 500 records)
python -m winstream run --events samples/events.jsonl --db out.db \
    --window tumbling:60 --delay 15 --lateness 600
python -m winstream show --db out.db --key dev-01 --limit 5
python -m winstream show --db out.db --changelog | head

# 3. simulate a crash, then re-run the same command to resume
python -m winstream run --events samples/events.jsonl --db crash.db --window session:120 --crash-after 700
python -m winstream run --events samples/events.jsonl --db crash.db --window session:120

# 4. check against the oracle, with 3 crash/restart replays
python -m winstream verify --events samples/events.jsonl --window session:120 --delay 15 --lateness 120

# 5. latency / completeness table
python -m winstream bench
```

`--no-flush` keeps windows open at end of input, so a later run over a grown
file continues where the last one stopped (incremental tailing). Without it the
watermark goes to +∞ at end of input and every window is emitted.

Window syntax: `tumbling:SECONDS`, `hopping:SIZE:SLIDE` (SIZE a multiple of
SLIDE), `session:GAP`. Event format, one JSON object per line:

```json
{"id":"e000370","key":"dev-02","ts":1767225600,"value":527,"arrival":1767225602}
```

`ts` is epoch seconds or ISO-8601 with an offset. `value` is an integer, so
sums are exact. `arrival` is optional and used only for latency reporting.

## Sample inputs and expected outputs

* `samples/tiny.jsonl`: 14 hand-written lines covering an in-order window, a
  re-delivered event, an event that is out of order but in time, a late event
  that updates a fired window, a late event past the horizon, an ISO timestamp
  and two invalid rows.
  * `samples/expected/tiny_tumbling60_{results,changelog}.tsv`: output of
    `run --window tumbling:60 --delay 10 --lateness 60`. Look for the
    `update ... revision 1` row and the late `a6`.
  * `samples/expected/tiny_session40_{results,changelog}.tsv`: the same input
    with `session:40`. Event `a6` (ts 1010, far behind the watermark) is
    accepted here because it falls inside the held session `[1000, 1125)`.
    `a8` would extend a session behind the watermark, so it is dropped.
* `samples/events.jsonl`: the generated stream used by `verify` in the tests.
* `samples/expected/bench.md`: output of `python -m winstream bench`.

`tests/test_cli.py` regenerates the tiny outputs and asserts they equal the
files byte for byte.

## Verification and measured results

`verify` on `samples/events.jsonl` (1214 rows). I ran it for each window type
with `delay ∈ {0, 15}`, `lateness ∈ {0, 600}`, plus `session:30` / `session:600` and
`delay 5 / lateness 60`. Every run reported `ok: true`: 0 missing, 0 extra and
0 wrong windows. The late side output equalled the oracle's set of late pairs,
the changelog replay equalled the results table, no insert came before the
watermark, and every crash/restart replay (4 injected crashes each, one of
them inside the commit transaction) gave output identical to the clean run.

Benchmark (`python -m winstream bench`: generator seed 7, 8 devices, 3 h,
2374 rows including re-deliveries). Each score is against the batch truth
over all unique events:

| window | mode | delay | lateness | first exact | final exact | spurious | dropped | corrections | p50 lat (s) | p95 lat (s) | peak state |
|---|---|---|---|---|---|---|---|---|---|---|---|
| tumbling:60 | processing-time | 0 | 0 | 54.5% | 54.5% | 30 | 0 | 0 | 2 | 21 | 7 |
| tumbling:60 | event-time | 0 | 0 | 81.4% | 81.4% | 0 | 277 | 0 | 5 | 24 | 7 |
| tumbling:60 | event-time | 15 | 0 | 84.5% | 84.5% | 0 | 238 | 0 | 19 | 35 | 11 |
| tumbling:60 | event-time | 60 | 0 | 87.9% | 87.9% | 0 | 188 | 0 | 65 | 84 | 15 |
| tumbling:60 | event-time | 180 | 0 | 95.7% | 95.7% | 0 | 97 | 0 | 186 | 205 | 27 |
| tumbling:60 | event-time | 15 | 600 | 85.7% | **100.0%** | 0 | 0 | 171 | 20 | 80 | 64 |
| session:120 | event-time | 0 | 0 | 5.7% | 5.7% | 38 | 444 | 0 | 7 | 37 | 8 |
| session:120 | event-time | 15 | 0 | 71.7% | 71.7% | 16 | 162 | 0 | 25 | 43 | 8 |
| session:120 | event-time | 180 | 0 | 81.1% | 81.1% | 12 | 99 | 0 | 185 | 208 | 9 |
| session:120 | event-time | 15 | 600 | 92.5% | **100.0%** | 0 | 0 | 76 | 40 | 358 | 11 |

(Full table, including hopping windows, in `samples/expected/bench.md`.)

What the numbers show, for this synthetic stream only:

* Bucketing by arrival time gets 54.5% of per-minute windows exactly right and
  invents 30 windows that do not exist in event time.
* With the watermark alone, a longer delay buys completeness with latency:
  going from 0 s to 180 s of delay lifts exact windows from 81.4% to 95.7%, about 12.6 s of extra latency per percentage point. Even a 180 s delay drops 97
  events, because the offline-device backlogs arrive minutes late.
* A short delay plus allowed lateness gives fast first results (p50 20 s) that
  converge to 100% exact. The cost is 171 correction rows and about 6× the
  open state.
* Session windows are hit hardest by drops. A dropped bridging event splits a
  session in two, which shows up in the "spurious" column. With lateness,
  sessions converge, and the p95 latency of 358 s is mostly merged sessions
  re-inserted under a new identity after a backlog arrived.

Metric definitions are in `winstream/bench.py`. "First exact" is the share of
true windows whose first emitted result for that exact identity already
matches the truth. Latency is in processing time: the trigger event's arrival
time minus the window end. Results emitted by the end-of-input flush are
excluded.

## Tests

```bash
python -m pytest -q
```

150 tests (about 1.5 s):

* `test_model.py`: parsing and validation, half-open window assignment,
  hopping coverage, accumulator merge and state round-trip.
* `test_engine.py`: hand-made scenarios. Firing exactly at the watermark;
  update then drop around the lateness horizon; boundary at `lateness = 0`;
  duplicates; the shared watermark across keys; a session bridge with two
  retractions; events inside held sessions accepted behind the watermark;
  update-in-place; flush; snapshot/restore every 17 events equal to an
  uninterrupted run.
* `test_oracle.py`: **100 randomised cross-checks** (5 window specs × 5
  delay/lateness settings × 4 seeds) of the operator against the oracle,
  covering results *and* the exact set of late pairs. Also checks that
  unbounded lateness converges to the batch truth, plus a brute-force check of
  the oracle's own window assignment.
* `test_pipeline.py`: oracle match through the SQLite sink, including an
  invalid and a blank line. Output stays identical across 13 crash points ×
  3-crash schedules, including a crash inside the commit and one at the last
  record. Also: rollback leaves the database untouched; incremental
  `--no-flush` runs equal a one-shot run; mismatched configs and input that
  grows after a flush are refused; changelog replay detects corruption.
* `test_cli.py`: the tiny sample against the expected files, `verify` on the
  bundled stream, crash then resume through the CLI, and benchmark sanity
  (event time with lateness reaches 100% and beats processing time; a longer
  delay costs latency).

## Design decisions

* **One transaction for output, state and offset.** That one transaction is
  what makes exactly-once work. There is no separate offset store that could
  drift from the data. Emissions are buffered in memory between commits, so a
  crash loses only uncommitted work, and the restart reproduces exactly that
  work because the operator is deterministic.
* **Changelog plus materialised table.** Downstream systems can consume the
  append-only changelog (insert / update / retract, with revisions), or they
  can read `results`. `verify` rebuilds `results` from the changelog to prove
  the two agree.
* **Integer values.** Sums are exact, so the oracle comparison can use
  equality instead of a tolerance.
* **The oracle shares no code with the engine.** It has its own window
  assignment and sessioniser and no incremental state. For the session "still
  held" check it re-sessionises everything accepted so far from scratch.
  That is O(n²) but obviously right.
* **A conservative session drop rule.** Flink's merging-window rule is more
  permissive, but it needs extra bookkeeping to prove that a late event cannot
  touch a purged session. Here an event behind `wm − lateness` is accepted
  only when it falls inside a held session, because only then is non-overlap
  with purged sessions guaranteed.
* **Heaps for firing and purging**, with lazy deletion of stale entries left
  behind by session merges, so each event costs O(log n) work rather than a scan of all state.

## Limitations

* The engine runs on a single process with a single input partition. It has
  no per-partition watermarks, no idle-source detection (a silent stream
  holds the watermark) and no parallelism.
* The whole operator state is serialised as JSON at each checkpoint, which is
  O(state) per commit. Accumulators keep their event ids for deduplication, so
  state grows with events per open window. A large job would need incremental
  snapshots and a bounded dedupe store.
* The aggregates are count, sum, min, max and mean only. No user-defined
  aggregates and no exact distinct count.
* There are no early (speculative) firings before the watermark, and a
  window's first result waits for the watermark.
* The session rule drops an event behind the watermark that would *extend* a
  held session. A more permissive rule would keep it. With `lateness = 0`
  this still drops many session events (see the benchmark).
* The source is a local JSONL file read in arrival order. There is no
  Kafka-style consumer. The benchmark numbers come from one synthetic stream
  and do not generalise to real traffic shapes.
