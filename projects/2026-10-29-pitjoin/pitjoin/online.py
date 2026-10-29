"""Online store: incremental materialization into SQLite and serving lookups.

The online store keeps, per view and key, the row a point-in-time join would
pick "now".  It is fed incrementally: each run admits source rows whose
``available_at`` falls in ``(watermark, end]`` and keeps the higher-ranked of
the stored and incoming row, so any sequence of runs ending at W yields the
same state as one run to W.  ``skew_check`` verifies that the served values
equal the offline point-in-time join evaluated at the watermark, which is
the training/serving consistency guarantee.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .engines import sweep_join
from .repo import FeatureView, format_ts


class MaterializeError(RuntimeError):
    pass


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS online_meta (view TEXT PRIMARY KEY, watermark INTEGER NOT NULL)")
    return conn


def _table(view: FeatureView) -> str:
    return f'"online__{view.name}"'


def _ensure_table(conn: sqlite3.Connection, view: FeatureView) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {_table(view)} (
        k TEXT PRIMARY KEY, ts INTEGER NOT NULL, created INTEGER NOT NULL,
        idx INTEGER NOT NULL, vals TEXT NOT NULL)""")


def watermark(conn: sqlite3.Connection, view_name: str) -> int | None:
    row = conn.execute("SELECT watermark FROM online_meta WHERE view = ?", (view_name,)).fetchone()
    return None if row is None else row[0]


def materialize(conn: sqlite3.Connection, views: dict[str, FeatureView], end: int,
                full: bool = False) -> dict[str, dict]:
    """Advance every view's online state to ``end``.  Returns per-view stats.

    ``full=True`` drops the view's online state first (a rebuild).  Moving the
    watermark backwards is refused: the store cannot un-see rows.
    """
    stats = {}
    with conn:
        for name, view in views.items():
            if full:
                conn.execute(f"DROP TABLE IF EXISTS {_table(view)}")
                conn.execute("DELETE FROM online_meta WHERE view = ?", (name,))
            _ensure_table(conn, view)
            start = watermark(conn, name)
            if start is not None and end < start:
                raise MaterializeError(
                    f"{name}: end {format_ts(end)} is before watermark {format_ts(start)}")
            batch = [r for r in view.rows
                     if r.available_at <= end and (start is None or r.available_at > start)]
            conn.executemany(
                f"""INSERT INTO {_table(view)} (k, ts, created, idx, vals) VALUES (?,?,?,?,?)
                    ON CONFLICT(k) DO UPDATE SET
                        ts = excluded.ts, created = excluded.created,
                        idx = excluded.idx, vals = excluded.vals
                    WHERE (excluded.ts, excluded.created, excluded.idx) > (ts, created, idx)""",
                ((r.key, r.ts, r.created, r.idx, json.dumps(list(r.values))) for r in batch))
            conn.execute(
                "INSERT INTO online_meta (view, watermark) VALUES (?, ?) "
                "ON CONFLICT(view) DO UPDATE SET watermark = excluded.watermark", (name, end))
            stats[name] = {"start": None if start is None else format_ts(start),
                           "end": format_ts(end), "rows_scanned": len(batch)}
    return stats


def get_online(conn: sqlite3.Connection, view: FeatureView, keys: list[str],
               now: int | None = None) -> list[dict]:
    """Serve features for ``keys``.  ``now`` defaults to the view's watermark.

    A stored row older than the view's TTL at ``now`` is served as nulls,
    matching what the offline join returns for an expired feature.
    """
    wm = watermark(conn, view.name)
    if wm is None:
        raise MaterializeError(f"{view.name}: not materialized yet")
    now = wm if now is None else now
    out = []
    for k in keys:
        row = conn.execute(f"SELECT ts, idx, vals FROM {_table(view)} WHERE k = ?", (k,)).fetchone()
        expired = row is not None and view.ttl_us is not None and row[0] < now - view.ttl_us
        if row is None or expired:
            out.append({"key": k, "idx": None, "features": dict.fromkeys(view.feature_names)})
        else:
            out.append({"key": k, "idx": row[1], "features": dict(zip(view.feature_names, json.loads(row[2])))})
    return out


def skew_check(conn: sqlite3.Connection, views: dict[str, FeatureView]) -> dict[str, dict]:
    """Compare online serving with the offline PIT join at the watermark, per key."""
    result = {}
    for name, view in views.items():
        wm = watermark(conn, name)
        if wm is None:
            result[name] = {"status": "not_materialized"}
            continue
        keys = sorted({r.key for r in view.rows})
        offline = sweep_join(view, keys, [wm] * len(keys), "knowledge")
        online = [o["idx"] for o in get_online(conn, view, keys, wm)]
        mismatches = [{"key": k, "offline_idx": a, "online_idx": b}
                      for k, a, b in zip(keys, offline, online) if a != b]
        result[name] = {"status": "ok" if not mismatches else "skew", "watermark": format_ts(wm),
                        "keys_checked": len(keys), "mismatches": mismatches}
    return result
