"""Snapshot staging + set-based SCD2 rebuild on SQLite.

Design: raw snapshots are the source of truth (``stg_snapshot``).  The
dimension table (``dim_scd2``) is a pure function of the staged snapshots and
is rebuilt with one SQL statement (gaps-and-islands over window functions).
That makes loads idempotent and order-independent: a late-arriving snapshot
is handled by simply rebuilding.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED = {"as_of", "row_hash", "sk", "valid_from", "valid_to", "is_current"}


class SnapshotError(ValueError):
    """Invalid snapshot input or a conflicting reload."""


def _ident(name: str) -> str:
    if not _IDENT.match(name) or name.lower() in RESERVED:
        raise SnapshotError(f"invalid or reserved column name: {name!r}")
    return f'"{name}"'


def _row_hash(values: list[str]) -> str:
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:16]


class Warehouse:
    def __init__(self, path: str | Path = ":memory:"):
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS snapshot_index ("
            " as_of TEXT PRIMARY KEY, row_count INTEGER NOT NULL, content_hash TEXT NOT NULL)"
        )
        self.con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.con.commit()

    # ---------------------------------------------------------------- meta
    def _meta(self, k: str) -> str | None:
        r = self.con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    @property
    def key(self) -> str | None:
        return self._meta("key")

    @property
    def columns(self) -> list[str]:
        v = self._meta("columns")
        return json.loads(v) if v else []

    # ---------------------------------------------------------------- load
    def load_csv(self, path: str | Path, as_of: str, key: str, replace: bool = False) -> str:
        """Load one snapshot. Returns 'loaded', 'unchanged' or 'replaced'."""
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header:
                raise SnapshotError("empty file: header row required")
            rows = [r for r in reader if r]
        return self.load_rows(header, rows, as_of, key, replace)

    def load_rows(self, header: list[str], rows: list[list[str]], as_of: str,
                  key: str, replace: bool = False) -> str:
        try:
            if len(as_of) != 10:
                raise ValueError
            date.fromisoformat(as_of)
        except ValueError:
            raise SnapshotError(f"as_of must be YYYY-MM-DD, got {as_of!r}") from None
        if key not in header:
            raise SnapshotError(f"key column {key!r} not in header {header}")
        if len(set(header)) != len(header):
            raise SnapshotError("duplicate column names in header")
        for h in header:
            _ident(h)
        attrs = [h for h in header if h != key]
        if not attrs:
            raise SnapshotError("need at least one attribute column besides the key")
        if self.key is not None and (self.key != key or sorted(self.columns) != sorted(attrs)):
            raise SnapshotError(
                f"schema mismatch: warehouse has key={self.key} columns={self.columns}"
            )
        ki = header.index(key)
        ai = [header.index(a) for a in attrs]
        recs: dict[str, list[str]] = {}
        for n, r in enumerate(rows, start=2):
            if len(r) != len(header):
                raise SnapshotError(f"line {n}: expected {len(header)} fields, got {len(r)}")
            k = r[ki].strip()
            if not k:
                raise SnapshotError(f"line {n}: empty key")
            if k in recs:
                raise SnapshotError(f"line {n}: duplicate key {k!r} in snapshot")
            recs[k] = [r[i].strip() for i in ai]
        content_hash = _row_hash([x for k in sorted(recs) for x in [k, *recs[k]]])

        existing = self.con.execute(
            "SELECT content_hash FROM snapshot_index WHERE as_of=?", (as_of,)
        ).fetchone()
        if existing and existing[0] == content_hash:
            return "unchanged"
        if existing and not replace:
            raise SnapshotError(
                f"snapshot {as_of} already loaded with different content (use replace=True)"
            )
        with self.con:  # single transaction
            if self.key is None:
                self.con.execute("INSERT INTO meta VALUES ('key', ?)", (key,))
                self.con.execute("INSERT INTO meta VALUES ('columns', ?)", (json.dumps(attrs),))
                cols = ", ".join(f"{_ident(a)} TEXT" for a in attrs)
                self.con.execute(
                    f"CREATE TABLE stg_snapshot (as_of TEXT NOT NULL, {_ident(key)} TEXT NOT NULL, "
                    f"{cols}, row_hash TEXT NOT NULL, PRIMARY KEY (as_of, {_ident(key)}))"
                )
            stored = self.columns
            self.con.execute("DELETE FROM stg_snapshot WHERE as_of=?", (as_of,))
            self.con.execute("DELETE FROM snapshot_index WHERE as_of=?", (as_of,))
            ph = ", ".join("?" * (len(stored) + 3))
            colsql = ", ".join(["as_of", _ident(self.key), *map(_ident, stored), "row_hash"])
            order = [attrs.index(c) for c in stored]
            self.con.executemany(
                f"INSERT INTO stg_snapshot ({colsql}) VALUES ({ph})",
                [(as_of, k, *[v[i] for i in order], _row_hash(v)) for k, v in recs.items()],
            )
            self.con.execute(
                "INSERT INTO snapshot_index VALUES (?,?,?)", (as_of, len(recs), content_hash)
            )
            self._rebuild()
        return "replaced" if existing else "loaded"

    # ------------------------------------------------------------- rebuild
    def _rebuild(self) -> None:
        k = _ident(self.key)
        cols = [_ident(c) for c in self.columns]
        sel = ", ".join(f"MIN(r.{c}) AS {c}" for c in cols)
        self.con.execute("DROP TABLE IF EXISTS dim_scd2")
        self.con.execute(
            f"""
            CREATE TABLE dim_scd2 AS
            WITH snaps AS (
                SELECT as_of, ROW_NUMBER() OVER (ORDER BY as_of) AS idx FROM snapshot_index
            ),
            r AS (
                SELECT s.*, n.idx FROM stg_snapshot s JOIN snaps n ON n.as_of = s.as_of
            ),
            flagged AS (
                SELECT r.*,
                       CASE WHEN LAG(idx) OVER w = idx - 1 AND LAG(row_hash) OVER w = row_hash
                            THEN 0 ELSE 1 END AS new_run
                FROM r WINDOW w AS (PARTITION BY {k} ORDER BY idx)
            ),
            runs AS (
                SELECT flagged.*, SUM(new_run) OVER (PARTITION BY {k} ORDER BY idx) AS run_id
                FROM flagged
            ),
            versions AS (
                SELECT r.{k} AS {k}, {sel},
                       MIN(r.as_of) AS valid_from, MAX(r.idx) AS last_idx
                FROM runs r GROUP BY r.{k}, r.run_id
            )
            SELECT ROW_NUMBER() OVER (ORDER BY v.{k}, v.valid_from) AS sk,
                   v.{k} AS {k}, {", ".join("v." + c for c in cols)},
                   v.valid_from AS valid_from,
                   nx.as_of AS valid_to,
                   CASE WHEN nx.as_of IS NULL THEN 1 ELSE 0 END AS is_current
            FROM versions v LEFT JOIN snaps nx ON nx.idx = v.last_idx + 1
            ORDER BY v.{k}, v.valid_from
            """
        )

    # ------------------------------------------------------------- queries
    def _need_data(self) -> None:
        if self.key is None:
            raise SnapshotError("warehouse is empty: load a snapshot first")

    def as_of(self, day: str) -> list[sqlite3.Row]:
        """Dimension state as it was after the last snapshot on or before ``day``."""
        self._need_data()
        return self.con.execute(
            f"SELECT {_ident(self.key)}, {', '.join(map(_ident, self.columns))} FROM dim_scd2 "
            "WHERE valid_from <= ? AND (valid_to IS NULL OR ? < valid_to) "
            f"ORDER BY {_ident(self.key)}",
            (day, day),
        ).fetchall()

    def history(self, key_value: str) -> list[sqlite3.Row]:
        self._need_data()
        return self.con.execute(
            f"SELECT * FROM dim_scd2 WHERE {_ident(self.key)} = ? ORDER BY valid_from", (key_value,)
        ).fetchall()

    def snapshots(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM snapshot_index ORDER BY as_of").fetchall()

    # -------------------------------------------------------------- checks
    def check(self) -> list[str]:
        """Return a list of invariant violations (empty list = healthy)."""
        self._need_data()
        k = _ident(self.key)
        problems: list[str] = []
        q = self.con.execute

        n = q("SELECT COUNT(*) FROM dim_scd2 WHERE valid_to IS NOT NULL AND valid_to <= valid_from").fetchone()[0]
        if n:
            problems.append(f"{n} version(s) with valid_to <= valid_from")

        n = q(
            f"SELECT COUNT(*) FROM (SELECT {k}, valid_from, "
            f"LAG(valid_to) OVER (PARTITION BY {k} ORDER BY valid_from) AS pv, "
            f"LAG(valid_to IS NULL) OVER (PARTITION BY {k} ORDER BY valid_from) AS pv_open "
            "FROM dim_scd2) WHERE pv_open = 1 OR pv > valid_from"
        ).fetchone()[0]
        if n:
            problems.append(f"{n} overlapping version(s)")

        n = q(f"SELECT COUNT(*) FROM (SELECT {k} FROM dim_scd2 WHERE valid_to IS NULL GROUP BY {k} HAVING COUNT(*) > 1)").fetchone()[0]
        if n:
            problems.append(f"{n} key(s) with more than one open version")

        last = q("SELECT MAX(as_of) FROM snapshot_index").fetchone()[0]
        n = q(
            f"SELECT COUNT(*) FROM (SELECT {k} FROM stg_snapshot WHERE as_of=? "
            f"EXCEPT SELECT {k} FROM dim_scd2 WHERE is_current=1)", (last,)
        ).fetchone()[0]
        m = q(
            f"SELECT COUNT(*) FROM (SELECT {k} FROM dim_scd2 WHERE is_current=1 "
            f"EXCEPT SELECT {k} FROM stg_snapshot WHERE as_of=?)", (last,)
        ).fetchone()[0]
        if n or m:
            problems.append(f"current rows disagree with latest snapshot ({n} missing, {m} extra)")

        # Strongest check: point-in-time reconstruction equals every snapshot.
        for s in self.snapshots():
            d = s["as_of"]
            cols = ", ".join(map(_ident, self.columns))
            a = q(
                f"SELECT COUNT(*) FROM (SELECT {k}, {cols} FROM stg_snapshot WHERE as_of=? "
                f"EXCEPT SELECT {k}, {cols} FROM dim_scd2 WHERE valid_from<=? AND (valid_to IS NULL OR ?<valid_to))",
                (d, d, d),
            ).fetchone()[0]
            b = q(
                f"SELECT COUNT(*) FROM (SELECT {k}, {cols} FROM dim_scd2 WHERE valid_from<=? AND (valid_to IS NULL OR ?<valid_to) "
                f"EXCEPT SELECT {k}, {cols} FROM stg_snapshot WHERE as_of=?)",
                (d, d, d),
            ).fetchone()[0]
            if a or b:
                problems.append(f"reconstruction of {d} differs from snapshot ({a} missing, {b} extra)")
        return problems
