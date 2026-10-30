"""Point-in-time join engines.

All engines answer the same question for every (entity row, feature view):
which source row would a model have seen at the entity row's timestamp T?

    eligible(f) := f.key == key
                   and f.ts <= T                       # no future facts
                   and (ttl is None or f.ts >= T - ttl)  # not stale
                   and (mode == "event" or f.created <= T)  # already ingested
    chosen      := argmax over eligible rows of (f.ts, f.created, f.idx)

``mode="knowledge"`` (default) is bitemporal: it also hides rows that had not
landed yet, such as a correction backfilled after T.  ``mode="event"`` only
looks at event time, the way most feature stores do; it is offered so the
leakage it lets through can be measured.

Engines return the chosen row's ``idx`` (or None), not values, so results can
be compared exactly and provenance kept.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from collections import defaultdict

from .repo import FeatureRow, FeatureView

MODES = ("knowledge", "event")


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")


def eligible(row: FeatureRow, key, t: int, ttl_us: int | None, mode: str) -> bool:
    return (
        row.key == key
        and row.ts <= t
        and (ttl_us is None or row.ts >= t - ttl_us)
        and (mode == "event" or row.created <= t)
    )


def oracle_join(view: FeatureView, keys: list, ts: list[int], mode: str = "knowledge") -> list[int | None]:
    """O(n*m) brute force straight from the definition.  Used as ground truth."""
    _check_mode(mode)
    out = []
    for key, t in zip(keys, ts):
        best = None
        for row in view.rows:
            if eligible(row, key, t, view.ttl_us, mode) and (best is None or row.rank > best.rank):
                best = row
        out.append(None if best is None else best.idx)
    return out


def sweep_join(view: FeatureView, keys: list, ts: list[int], mode: str = "knowledge") -> list[int | None]:
    """Sort-merge sweep, O((n + m) log(n + m)).

    Per key, both constraints ``ts <= T`` and ``created <= T`` are of the form
    ``something <= T``, so a row becomes eligible at ``available_at`` and never
    stops being eligible as T grows (TTL aside).  Walking entity rows in time
    order and admitting feature rows by ``available_at`` therefore reduces the
    argmax to a running maximum.  TTL needs no extra state: if the running
    best (the newest eligible event) is too old, every other eligible row is
    older still.
    """
    _check_mode(mode)
    avail = (lambda r: r.ts) if mode == "event" else (lambda r: r.available_at)
    by_key: dict = defaultdict(list)
    for row in view.rows:
        by_key[row.key].append(row)
    for rows in by_key.values():
        rows.sort(key=avail)

    queries: dict = defaultdict(list)
    for i, (key, t) in enumerate(zip(keys, ts)):
        if key is not None and key in by_key:
            queries[key].append((t, i))

    out: list[int | None] = [None] * len(keys)
    ttl = view.ttl_us
    for key, qs in queries.items():
        qs.sort()
        rows = by_key[key]
        p, best = 0, None
        for t, i in qs:
            while p < len(rows) and avail(rows[p]) <= t:
                if best is None or rows[p].rank > best.rank:
                    best = rows[p]
                p += 1
            if best is not None and (ttl is None or best.ts >= t - ttl):
                out[i] = best.idx
    return out


def sql_join(view: FeatureView, keys: list, ts: list[int], mode: str = "knowledge",
             conn: sqlite3.Connection | None = None) -> list[int | None]:
    """The same join expressed as warehouse SQL (SQLite window functions).

    This is the shape you would run in a warehouse: a range join on key and
    time, then ROW_NUMBER() to keep the newest eligible row per entity row.
    """
    _check_mode(mode)
    own = conn is None
    conn = conn or sqlite3.connect(":memory:")
    try:
        conn.execute("DROP TABLE IF EXISTS temp.pit_features")
        conn.execute("DROP TABLE IF EXISTS temp.pit_spine")
        conn.execute("CREATE TEMP TABLE pit_features (idx INTEGER PRIMARY KEY, k TEXT, ts INTEGER, created INTEGER)")
        conn.execute("CREATE TEMP TABLE pit_spine (row_id INTEGER PRIMARY KEY, k TEXT, ts INTEGER)")
        conn.executemany("INSERT INTO pit_features VALUES (?,?,?,?)",
                         ((r.idx, r.key, r.ts, r.created) for r in view.rows))
        conn.executemany("INSERT INTO pit_spine VALUES (?,?,?)",
                         ((i, k, t) for i, (k, t) in enumerate(zip(keys, ts))))
        conn.execute("CREATE INDEX temp.pit_features_k_ts ON pit_features (k, ts)")
        sql = """
            SELECT row_id, idx FROM (
                SELECT s.row_id, f.idx,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.row_id
                           ORDER BY f.ts DESC, f.created DESC, f.idx DESC) AS rn
                FROM pit_spine s
                JOIN pit_features f
                  ON f.k = s.k
                 AND f.ts <= s.ts
                 AND (:ttl IS NULL OR f.ts >= s.ts - :ttl)
                 AND (:event_mode = 1 OR f.created <= s.ts)
            ) WHERE rn = 1
        """
        out: list[int | None] = [None] * len(keys)
        params = {"ttl": view.ttl_us, "event_mode": 1 if mode == "event" else 0}
        for row_id, idx in conn.execute(sql, params):
            out[row_id] = idx
        return out
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.pit_features")
        conn.execute("DROP TABLE IF EXISTS temp.pit_spine")
        if own:
            conn.close()


def naive_latest_join(view: FeatureView, keys: list, ts: list[int], mode: str = "knowledge") -> list[int | None]:
    """Baseline: join today's latest value per key, ignoring T entirely.

    This is what ``df.merge(latest_features, on="customer_id")`` does.  It is
    correct for online scoring *now* and wrong for every historical label.
    ``mode`` is accepted for interface symmetry and ignored.
    """
    latest: dict = {}
    for row in view.rows:
        cur = latest.get(row.key)
        if cur is None or row.rank > cur.rank:
            latest[row.key] = row
    return [None if (k is None or k not in latest) else latest[k].idx for k in keys]


def asof_bisect_join(view: FeatureView, keys: list, ts: list[int], mode: str = "knowledge") -> list[int | None]:
    """Event-time-only as-of join (pandas ``merge_asof`` semantics), for reference.

    Per key it binary-searches the newest row with ``ts <= T``.  It ignores
    created timestamps whatever ``mode`` says, so it equals ``mode="event"``
    only; it is here to show that a textbook as-of join is not bitemporal.
    """
    by_key: dict = defaultdict(list)
    for row in view.rows:
        by_key[row.key].append(row)
    index = {}
    for k, rows in by_key.items():
        rows.sort(key=lambda r: r.rank)
        index[k] = ([r.ts for r in rows], rows)
    out: list[int | None] = []
    ttl = view.ttl_us
    for k, t in zip(keys, ts):
        if k not in index:
            out.append(None)
            continue
        tss, rows = index[k]
        # rows sorted by (ts, created, idx), so the last with ts <= t is the max rank
        pos = bisect_right(tss, t) - 1
        if pos < 0 or (ttl is not None and rows[pos].ts < t - ttl):
            out.append(None)
        else:
            out.append(rows[pos].idx)
    return out


ENGINES = {
    "sweep": sweep_join,
    "sql": sql_join,
    "oracle": oracle_join,
}
BASELINES = {
    "naive_latest": naive_latest_join,
    "asof_event_only": asof_bisect_join,
}
