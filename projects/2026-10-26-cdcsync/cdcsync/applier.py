"""Apply a CDC event stream to a SQLite target.

Guarantees (see README for the reasoning):

* Convergence under reordering and duplication: every row stores the LSN of
  the change that produced it and a change is applied only if its LSN is
  strictly greater (last-writer-wins by LSN). Deletes leave a tombstone so a
  late, older create/update cannot resurrect the row.
* Transaction atomicity: changes that belong to a transaction are staged and
  applied together once the tx_end marker and all `event_count` changes have
  arrived, in any order.
* Crash safety / exactly-once effect: each batch of input lines is processed
  in one SQLite transaction that also advances the per-source line offset and
  persists staged (incomplete) transactions. A crash rolls the whole batch
  back; the next run resumes at the last committed offset.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from .events import META_PREFIX, Change, RejectError, TxEnd, parse_line

LSN_COL = "_cdc_lsn"
DELETED_COL = "_cdc_deleted"

CONTROL_DDL = """
CREATE TABLE IF NOT EXISTS _cdc_offsets (source TEXT PRIMARY KEY, line INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS _cdc_tables (name TEXT PRIMARY KEY, pk_cols TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _cdc_staging (
    tx TEXT NOT NULL, lsn INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (tx, lsn));
CREATE TABLE IF NOT EXISTS _cdc_tx_end (tx TEXT PRIMARY KEY, event_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS _cdc_applied_tx (
    tx TEXT PRIMARY KEY, event_count INTEGER NOT NULL, max_lsn INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS _cdc_rejects (
    source TEXT NOT NULL, line INTEGER NOT NULL, reason TEXT NOT NULL, raw TEXT NOT NULL,
    PRIMARY KEY (source, line));
"""


class SimulatedCrash(RuntimeError):
    """Raised by the crash hook in tests to abort a batch mid-way."""


@dataclass
class RunStats:
    lines_read: int = 0
    applied: int = 0
    stale_skipped: int = 0
    duplicates: int = 0
    rejected: int = 0
    tx_committed: int = 0
    pending_tx: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def q(name: str) -> str:
    """Quote an identifier that has already been validated by events.py."""
    return '"' + name.replace('"', '""') + '"'


class Applier:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(CONTROL_DDL)
        self._columns: dict[str, list[str]] = {}
        self._pks: dict[str, list[str]] = {
            name: json.loads(pk) for name, pk in self.conn.execute("SELECT name, pk_cols FROM _cdc_tables")
        }

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ schema
    def _table_columns(self, table: str) -> list[str]:
        if table not in self._columns:
            rows = self.conn.execute(f"PRAGMA table_info({q(table)})").fetchall()
            self._columns[table] = [r[1] for r in rows]
        return self._columns[table]

    def _ensure_table(self, change: Change) -> None:
        pk_cols = sorted(change.key)
        known = self._pks.get(change.table)
        if known is None:
            cols = ", ".join(q(c) for c in pk_cols)
            self.conn.execute(
                f"CREATE TABLE {q(change.table)} ({cols}, {LSN_COL} INTEGER NOT NULL, "
                f"{DELETED_COL} INTEGER NOT NULL DEFAULT 0, PRIMARY KEY ({cols}))"
            )
            self.conn.execute(
                "INSERT INTO _cdc_tables (name, pk_cols) VALUES (?, ?)", (change.table, json.dumps(pk_cols))
            )
            self._pks[change.table] = pk_cols
            self._columns.pop(change.table, None)
            self._refresh_view(change.table)
        elif known != pk_cols:
            raise RejectError(f"key columns {pk_cols} differ from table key {known}")

        if change.after:
            existing = set(self._table_columns(change.table))
            new_cols = [c for c in change.after if c not in existing]
            for col in new_cols:  # additive schema evolution only
                self.conn.execute(f"ALTER TABLE {q(change.table)} ADD COLUMN {q(col)}")
            if new_cols:
                self._columns.pop(change.table, None)
                self._refresh_view(change.table)

    def _refresh_view(self, table: str) -> None:
        data_cols = self.data_columns(table)
        self.conn.execute(f"DROP VIEW IF EXISTS {q('v_' + table)}")
        self.conn.execute(
            f"CREATE VIEW {q('v_' + table)} AS SELECT {', '.join(q(c) for c in data_cols)} "
            f"FROM {q(table)} WHERE {DELETED_COL} = 0"
        )

    def data_columns(self, table: str) -> list[str]:
        return [c for c in self._table_columns(table) if not c.startswith(META_PREFIX)]

    def tables(self) -> list[str]:
        return sorted(self._pks)

    # ------------------------------------------------------------------- apply
    def _apply_change(self, change: Change, stats: RunStats, guard: bool = True) -> None:
        self._ensure_table(change)
        pk_cols = self._pks[change.table]
        where = " AND ".join(f"{q(c)} = ?" for c in pk_cols)
        key_vals = [change.key[c] for c in pk_cols]
        row = self.conn.execute(
            f"SELECT {LSN_COL} FROM {q(change.table)} WHERE {where}", key_vals
        ).fetchone()
        if guard and row is not None and row[0] >= change.lsn:
            stats.stale_skipped += 1
            return

        if change.op == "d":
            values = dict(change.key)
            deleted = 1
        else:
            values = dict(change.after or {})
            deleted = 0
        cols = self.data_columns(change.table)
        # Columns absent from a full row image did not exist when it was written.
        params = [values.get(c) for c in cols] + [change.lsn, deleted]
        all_cols = cols + [LSN_COL, DELETED_COL]
        updates = ", ".join(f"{q(c)} = excluded.{q(c)}" for c in all_cols if c not in pk_cols)
        self.conn.execute(
            f"INSERT INTO {q(change.table)} ({', '.join(q(c) for c in all_cols)}) "
            f"VALUES ({', '.join('?' for _ in all_cols)}) "
            f"ON CONFLICT ({', '.join(q(c) for c in pk_cols)}) DO UPDATE SET {updates}",
            params,
        )
        stats.applied += 1

    def _is_applied(self, tx: str) -> bool:
        return self.conn.execute("SELECT 1 FROM _cdc_applied_tx WHERE tx = ?", (tx,)).fetchone() is not None

    def _try_commit_tx(self, tx: str, stats: RunStats) -> None:
        end = self.conn.execute("SELECT event_count FROM _cdc_tx_end WHERE tx = ?", (tx,)).fetchone()
        if end is None:
            return
        staged = self.conn.execute(
            "SELECT lsn, payload FROM _cdc_staging WHERE tx = ? ORDER BY lsn", (tx,)
        ).fetchall()
        if len(staged) < end[0]:
            return
        if len(staged) > end[0]:
            # More distinct changes than the producer announced: never apply a
            # transaction we cannot prove is complete and correct.
            return
        for _, payload in staged:
            self._apply_change(parse_line(payload), stats)
        self.conn.execute("DELETE FROM _cdc_staging WHERE tx = ?", (tx,))
        self.conn.execute("DELETE FROM _cdc_tx_end WHERE tx = ?", (tx,))
        self.conn.execute(
            "INSERT INTO _cdc_applied_tx (tx, event_count, max_lsn) VALUES (?, ?, ?)",
            (tx, end[0], staged[-1][0]),
        )
        stats.tx_committed += 1

    def _handle(self, rec: Change | TxEnd, stats: RunStats) -> None:
        if isinstance(rec, TxEnd):
            if self._is_applied(rec.tx):
                stats.duplicates += 1
                return
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO _cdc_tx_end (tx, event_count) VALUES (?, ?)", (rec.tx, rec.event_count)
            )
            if cur.rowcount == 0:
                stats.duplicates += 1
                return
            self._try_commit_tx(rec.tx, stats)
        elif rec.tx is None:
            self._apply_change(rec, stats)
        else:
            if self._is_applied(rec.tx):
                stats.duplicates += 1
                return
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO _cdc_staging (tx, lsn, payload) VALUES (?, ?, ?)",
                (rec.tx, rec.lsn, rec.to_json()),
            )
            if cur.rowcount == 0:
                stats.duplicates += 1
                return
            self._try_commit_tx(rec.tx, stats)

    def offset(self, source: str) -> int:
        row = self.conn.execute("SELECT line FROM _cdc_offsets WHERE source = ?", (source,)).fetchone()
        return row[0] if row else 0

    def apply_lines(
        self,
        lines: Iterable[str],
        source: str,
        batch_size: int = 500,
        crash_hook: Callable[[int], None] | None = None,
    ) -> RunStats:
        """Apply input lines, resuming after the committed offset for `source`.

        `crash_hook(line_no)` is called after each line is processed and may
        raise to simulate a crash before the batch commits.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        stats = RunStats()
        start = self.offset(source)
        line_no = 0
        in_batch = 0
        for raw in lines:
            line_no += 1
            if line_no <= start:
                continue
            if in_batch == 0:
                self.conn.execute("BEGIN IMMEDIATE")
                snapshot = RunStats(**stats.as_dict())
            try:
                self._process_line(raw, line_no, source, stats)
                in_batch += 1
                if crash_hook:
                    crash_hook(line_no)
                if in_batch >= batch_size:
                    self._commit_batch(source, line_no)
                    in_batch = 0
            except BaseException:
                self.conn.execute("ROLLBACK")
                self._columns.clear()
                self._reload_pks()
                stats.__dict__.update(snapshot.as_dict())
                raise
        if in_batch:
            self._commit_batch(source, line_no)
        stats.pending_tx = self.pending_tx_count()
        return stats

    def _process_line(self, raw: str, line_no: int, source: str, stats: RunStats) -> None:
        raw = raw.rstrip("\n")
        stats.lines_read += 1
        if not raw.strip():
            return
        try:
            rec = parse_line(raw)
            self.conn.execute("SAVEPOINT rec")
            try:
                self._handle(rec, stats)
            except RejectError:
                self.conn.execute("ROLLBACK TO rec")
                self._columns.clear()  # DDL may have been undone
                self._reload_pks()
                raise
            finally:
                self.conn.execute("RELEASE rec")
        except RejectError as exc:
            stats.rejected += 1
            self.conn.execute(
                "INSERT OR REPLACE INTO _cdc_rejects (source, line, reason, raw) VALUES (?, ?, ?, ?)",
                (source, line_no, str(exc), raw[:2000]),
            )

    def _commit_batch(self, source: str, line_no: int) -> None:
        self.conn.execute(
            "INSERT INTO _cdc_offsets (source, line) VALUES (?, ?) "
            "ON CONFLICT (source) DO UPDATE SET line = excluded.line",
            (source, line_no),
        )
        self.conn.execute("COMMIT")

    def _reload_pks(self) -> None:
        self._pks = {
            name: json.loads(pk) for name, pk in self.conn.execute("SELECT name, pk_cols FROM _cdc_tables")
        }

    def apply_file(self, path: str | Path, source: str | None = None, **kwargs) -> RunStats:
        path = Path(path)
        with path.open(encoding="utf-8") as fh:
            return self.apply_lines(fh, source or path.name, **kwargs)

    # ------------------------------------------------------------------ status
    def pending_tx_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM (SELECT tx FROM _cdc_staging UNION SELECT tx FROM _cdc_tx_end)"
        ).fetchone()[0]

    def status(self) -> dict:
        pending = self.conn.execute(
            """
            SELECT t.tx, e.event_count, COUNT(s.lsn)
            FROM (SELECT tx FROM _cdc_staging UNION SELECT tx FROM _cdc_tx_end) t
            LEFT JOIN _cdc_tx_end e ON e.tx = t.tx
            LEFT JOIN _cdc_staging s ON s.tx = t.tx
            GROUP BY t.tx ORDER BY t.tx
            """
        ).fetchall()
        tables = {}
        for t in self.tables():
            live, dead, max_lsn = self.conn.execute(
                f"SELECT COALESCE(SUM({DELETED_COL} = 0), 0), COALESCE(SUM({DELETED_COL}), 0), "
                f"MAX({LSN_COL}) FROM {q(t)}"
            ).fetchone()
            tables[t] = {"live_rows": live, "tombstones": dead, "max_lsn": max_lsn,
                         "columns": self.data_columns(t)}
        return {
            "offsets": dict(self.conn.execute("SELECT source, line FROM _cdc_offsets ORDER BY source")),
            "tables": tables,
            "applied_tx": self.conn.execute("SELECT COUNT(*) FROM _cdc_applied_tx").fetchone()[0],
            "pending_tx": [{"tx": tx, "expected": exp, "staged": got} for tx, exp, got in pending],
            "rejects": [
                {"source": s, "line": n, "reason": r}
                for s, n, r in self.conn.execute(
                    "SELECT source, line, reason FROM _cdc_rejects ORDER BY source, line"
                )
            ],
        }

    def dump(self) -> dict[str, list[dict]]:
        """Current (non-deleted) state of every replicated table, sorted by key."""
        out = {}
        for t in self.tables():
            cols = self.data_columns(t)
            order = ", ".join(q(c) for c in self._pks[t])
            cur = self.conn.execute(f"SELECT * FROM {q('v_' + t)} ORDER BY {order}")
            out[t] = [dict(zip(cols, r)) for r in cur]
        return out


class NaiveApplier(Applier):
    """Baseline for comparison: applies every change in arrival order.

    No LSN guard and no transaction buffering, i.e. what a straightforward
    "upsert each message" consumer does. Same storage layout, so the
    reconciler can measure how far it drifts from the source.
    """

    def _handle(self, rec: Change | TxEnd, stats: RunStats) -> None:
        if isinstance(rec, TxEnd):
            return
        self._apply_change(rec, stats, guard=False)
