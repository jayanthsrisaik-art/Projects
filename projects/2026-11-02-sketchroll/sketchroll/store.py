"""SQLite rollup store: idempotent batch ingest, compaction, range queries.

Layout
------
``rollups(endpoint, grain, start, ...)`` holds one row per endpoint per hour
(``grain='hour'``) or per day (``grain='day'``) with exact counters and the two
sketches as blobs. Invariant: for any (endpoint, day) there are hour rows *or*
one day row, never both. Ingest and compaction both maintain it, and
:func:`check_invariants` verifies it.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from .ddsketch import DDSketch
from .events import DAY, InvalidEvent, day_floor, fmt_ts, hour_floor, parse_ts, read_jsonl
from .hll import HyperLogLog
from .rollup import Rollup

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    accepted    INTEGER NOT NULL,
    rejected    INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rollups (
    endpoint TEXT NOT NULL,
    grain    TEXT NOT NULL CHECK (grain IN ('hour', 'day')),
    start    TEXT NOT NULL,
    n        INTEGER NOT NULL,
    errors   INTEGER NOT NULL,
    lat_sum  REAL NOT NULL,
    lat_min  REAL NOT NULL,
    lat_max  REAL NOT NULL,
    hll      BLOB NOT NULL,
    dds      BLOB NOT NULL,
    PRIMARY KEY (endpoint, grain, start)
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL,
    line_no  INTEGER NOT NULL,
    reason   TEXT NOT NULL,
    raw      TEXT NOT NULL
);
"""


class BatchConflict(RuntimeError):
    """Same batch id seen before with different content."""


class GranularityError(ValueError):
    """The query range cuts through a compacted day; the answer would be inexact."""


@dataclass
class IngestResult:
    batch_id: str
    status: str  # "ingested" | "skipped_duplicate"
    accepted: int = 0
    rejected: int = 0
    cells_touched: int = 0


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _load_row(row) -> Rollup:
    n, errors, lat_sum, lat_min, lat_max, hll, dds = row
    return Rollup(n, errors, lat_sum, lat_min, lat_max, HyperLogLog.from_bytes(hll), DDSketch.from_bytes(dds))


def _get(conn, endpoint: str, grain: str, start: str) -> Rollup | None:
    row = conn.execute(
        "SELECT n, errors, lat_sum, lat_min, lat_max, hll, dds FROM rollups WHERE endpoint=? AND grain=? AND start=?",
        (endpoint, grain, start)).fetchone()
    return _load_row(row) if row else None


def _put(conn, endpoint: str, grain: str, start: str, r: Rollup) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO rollups VALUES (?,?,?,?,?,?,?,?,?,?)",
        (endpoint, grain, start, r.n, r.errors, r.lat_sum, r.lat_min, r.lat_max,
         r.users.to_bytes(), r.latency.to_bytes()))


def ingest_file(conn: sqlite3.Connection, path: str, batch_id: str | None = None) -> IngestResult:
    """Ingest one JSONL batch atomically.

    The batch id defaults to the file's SHA-256, so re-delivering the same file is
    a no-op. Counts are not idempotent under re-adding (HLL is, counters are not),
    so the batch ledger is what makes ingestion safe to retry.
    """
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    batch_id = batch_id or digest
    seen = conn.execute("SELECT sha256 FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if seen:
        if seen[0] != digest:
            raise BatchConflict(f"batch {batch_id!r} was already ingested with different content")
        return IngestResult(batch_id, "skipped_duplicate")

    cells: dict[tuple[str, datetime], Rollup] = defaultdict(Rollup)
    rejects: list[tuple[int, str, str]] = []
    accepted = 0
    for line_no, raw, item in read_jsonl(path):
        if isinstance(item, InvalidEvent):
            rejects.append((line_no, str(item), raw))
            continue
        cells[(item.endpoint, hour_floor(item.ts))].add(item)
        accepted += 1

    with conn:  # one transaction: ledger row, quarantine and rollups commit together
        for (endpoint, hour), part in cells.items():
            day = fmt_ts(day_floor(hour))
            if _get(conn, endpoint, "day", day) is not None:
                # Late data for an already-compacted day merges into the day row.
                grain, start = "day", day
            else:
                grain, start = "hour", fmt_ts(hour)
            existing = _get(conn, endpoint, grain, start)
            _put(conn, endpoint, grain, start, existing.merge(part) if existing else part)
        conn.executemany("INSERT INTO quarantine VALUES (?,?,?,?)",
                         [(batch_id, ln, reason, raw) for ln, reason, raw in rejects])
        conn.execute("INSERT INTO batches VALUES (?,?,?,?,?)",
                     (batch_id, digest, accepted, len(rejects), fmt_ts(datetime.now(timezone.utc))))
    return IngestResult(batch_id, "ingested", accepted, len(rejects), len(cells))


def compact(conn: sqlite3.Connection, before: datetime) -> int:
    """Fold hour rows of every day that ends at or before ``before`` into day rows.

    Returns the number of day rows written. Sketch merges are exact, so any query
    aligned to whole days returns the same answer before and after compaction.
    """
    before = before.astimezone(timezone.utc)
    rows = conn.execute(
        "SELECT endpoint, start FROM rollups WHERE grain='hour' ORDER BY endpoint, start").fetchall()
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for endpoint, start in rows:
        day = day_floor(parse_ts(start))
        if day + DAY <= before:
            groups[(endpoint, fmt_ts(day))].append(start)
    with conn:
        for (endpoint, day), starts in groups.items():
            merged = _get(conn, endpoint, "day", day) or Rollup()
            for s in starts:
                merged.merge(_get(conn, endpoint, "hour", s))
            _put(conn, endpoint, "day", day, merged)
            conn.executemany("DELETE FROM rollups WHERE endpoint=? AND grain='hour' AND start=?",
                             [(endpoint, s) for s in starts])
    return len(groups)


def query(conn: sqlite3.Connection, start: datetime, end: datetime,
          endpoints: list[str] | None = None) -> Rollup:
    """Merge the cells covering ``[start, end)``; both bounds must be on the hour.

    Raises GranularityError when the range only partly covers a compacted day.
    """
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if hour_floor(start) != start or hour_floor(end) != end:
        raise ValueError("query bounds must be aligned to whole hours")
    if end <= start:
        raise ValueError("end must be after start")
    s, e = fmt_ts(start), fmt_ts(end)
    where_ep, params = "", []
    if endpoints:
        where_ep = f" AND endpoint IN ({','.join('?' * len(endpoints))})"
        params = list(endpoints)

    total = Rollup()
    # Day rows overlapping the range: must be fully inside it.
    for start_txt, *row in conn.execute(
            "SELECT start, n, errors, lat_sum, lat_min, lat_max, hll, dds FROM rollups "
            f"WHERE grain='day' AND start < ? AND start > ?{where_ep}",
            [e, fmt_ts(start - DAY)] + params):
        d = parse_ts(start_txt)
        if d < start or d + DAY > end:
            raise GranularityError(
                f"day {start_txt[:10]} is compacted to daily grain; query whole days or before compaction")
        total.merge(_load_row(row))
    for row in conn.execute(
            "SELECT n, errors, lat_sum, lat_min, lat_max, hll, dds FROM rollups "
            f"WHERE grain='hour' AND start >= ? AND start < ?{where_ep}", [s, e] + params):
        total.merge(_load_row(row))
    return total


def endpoints(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT endpoint FROM rollups ORDER BY endpoint")]


def check_invariants(conn: sqlite3.Connection) -> list[str]:
    """Return a list of human-readable violations (empty means healthy)."""
    problems = []
    for endpoint, start in conn.execute(
            "SELECT endpoint, start FROM rollups WHERE grain='hour'"):
        dt = parse_ts(start)
        if hour_floor(dt) != dt:
            problems.append(f"hour row {endpoint} {start} not on the hour")
        if conn.execute("SELECT 1 FROM rollups WHERE endpoint=? AND grain='day' AND start=?",
                        (endpoint, fmt_ts(day_floor(dt)))).fetchone():
            problems.append(f"{endpoint} {start}: hour row coexists with its day row")
    for endpoint, grain, start, n, *rest in conn.execute(
            "SELECT endpoint, grain, start, n, errors, lat_min, lat_max, dds FROM rollups"):
        errors, lat_min, lat_max, dds = rest
        sketch = DDSketch.from_bytes(dds)
        if sketch.count != n:
            problems.append(f"{endpoint} {grain} {start}: sketch count {sketch.count} != n {n}")
        if not 0 <= errors <= n or (n and lat_min > lat_max):
            problems.append(f"{endpoint} {grain} {start}: inconsistent counters")
        if grain == "day" and day_floor(parse_ts(start)) != parse_ts(start):
            problems.append(f"day row {endpoint} {start} not at midnight")
    ledger = conn.execute("SELECT COALESCE(SUM(accepted),0) FROM batches").fetchone()[0]
    stored = conn.execute("SELECT COALESCE(SUM(n),0) FROM rollups").fetchone()[0]
    if ledger != stored:
        problems.append(f"ledger accepted={ledger} but rollups hold n={stored}")
    return problems


def storage_bytes(conn: sqlite3.Connection) -> dict:
    rows, hll, dds = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(hll)),0), COALESCE(SUM(LENGTH(dds)),0) FROM rollups").fetchone()
    return {"rollup_rows": rows, "hll_bytes": hll, "ddsketch_bytes": dds}


