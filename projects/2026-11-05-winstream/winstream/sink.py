"""SQLite sink with exactly-once checkpoints.

Each commit writes, in ONE transaction:

* the changelog rows emitted since the last commit (append-only, ``seq``),
* their effect on the materialised ``results`` table (upsert / delete),
* side outputs (late, duplicate and invalid records),
* the operator state snapshot and the source offset to resume from.

If the process dies before COMMIT, SQLite rolls everything back and a restart
resumes from the previous offset with the previous state, re-producing exactly
the emissions that were lost. Nothing is written twice and nothing is missed.
"""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS changelog (
    seq INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('insert','update','retract')),
    key TEXT NOT NULL, wstart INTEGER NOT NULL, wend INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    count INTEGER, sum INTEGER, min INTEGER, max INTEGER, mean REAL,
    wm INTEGER, trigger_offset INTEGER, trigger_arrival INTEGER
);
CREATE TABLE IF NOT EXISTS results (
    key TEXT NOT NULL, wstart INTEGER NOT NULL, wend INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    count INTEGER NOT NULL, sum INTEGER NOT NULL, min INTEGER, max INTEGER, mean REAL,
    PRIMARY KEY (key, wstart, wend)
);
CREATE TABLE IF NOT EXISTS side_output (
    id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL, offset INTEGER, event_id TEXT, key TEXT, ts INTEGER,
    wstart INTEGER, wend INTEGER, wm INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS checkpoint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_offset INTEGER NOT NULL,
    next_seq INTEGER NOT NULL,
    config TEXT NOT NULL,
    state TEXT NOT NULL
);
"""

CHANGELOG_COLS = ("seq", "kind", "key", "wstart", "wend", "revision", "count", "sum",
                  "min", "max", "mean", "wm", "trigger_offset", "trigger_arrival")


class CrashInjected(RuntimeError):
    """Raised by tests / --crash-* flags to simulate a process dying."""


class Sink:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def load_checkpoint(self):
        row = self.conn.execute(
            "SELECT next_offset, next_seq, config, state FROM checkpoint WHERE id = 1").fetchone()
        if row is None:
            return None
        return {"next_offset": row[0], "next_seq": row[1],
                "config": json.loads(row[2]), "state": json.loads(row[3])}

    def commit(self, emissions, side_outputs, next_offset, next_seq_start, config, state,
               crash_before_commit=False):
        """Atomically persist a batch. Returns the next free changelog seq."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            seq = next_seq_start
            for rec in emissions:
                c.execute(
                    f"INSERT INTO changelog ({','.join(CHANGELOG_COLS)}) "
                    f"VALUES ({','.join('?' * len(CHANGELOG_COLS))})",
                    (seq, rec["kind"], rec["key"], rec["wstart"], rec["wend"], rec["revision"],
                     rec["count"], rec["sum"], rec["min"], rec["max"], rec["mean"], rec["wm"],
                     rec["trigger_offset"], rec["trigger_arrival"]))
                seq += 1
                apply_to_results(c, rec)
            for s in side_outputs:
                c.execute(
                    "INSERT INTO side_output (reason, offset, event_id, key, ts, wstart, wend, wm, detail)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["reason"], s.get("offset"), s.get("event_id"), s.get("key"), s.get("ts"),
                     s.get("wstart"), s.get("wend"), s.get("wm"), s.get("detail")))
            c.execute(
                "INSERT INTO checkpoint (id, next_offset, next_seq, config, state) VALUES (1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET next_offset=excluded.next_offset, "
                "next_seq=excluded.next_seq, config=excluded.config, state=excluded.state",
                (next_offset, seq, json.dumps(config, sort_keys=True),
                 json.dumps(state, separators=(",", ":"))))
            if crash_before_commit:
                raise CrashInjected("crash injected inside the commit transaction")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return seq

    # -------------------------------------------------------------- queries
    def results(self):
        return self.conn.execute(
            "SELECT key, wstart, wend, count, sum, min, max, mean, revision FROM results "
            "ORDER BY key, wstart, wend").fetchall()

    def changelog(self):
        cur = self.conn.execute(f"SELECT {','.join(CHANGELOG_COLS)} FROM changelog ORDER BY seq")
        return [dict(zip(CHANGELOG_COLS, r)) for r in cur.fetchall()]

    def side_outputs(self, reason=None):
        q = "SELECT reason, offset, event_id, key, ts, wstart, wend, wm, detail FROM side_output"
        args = ()
        if reason:
            q += " WHERE reason = ?"
            args = (reason,)
        q += " ORDER BY id"
        cols = ("reason", "offset", "event_id", "key", "ts", "wstart", "wend", "wm", "detail")
        return [dict(zip(cols, r)) for r in self.conn.execute(q, args).fetchall()]


def apply_to_results(c, rec):
    if rec["kind"] == "retract":
        c.execute("DELETE FROM results WHERE key=? AND wstart=? AND wend=?",
                  (rec["key"], rec["wstart"], rec["wend"]))
    else:
        c.execute(
            "INSERT INTO results (key, wstart, wend, revision, count, sum, min, max, mean) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(key, wstart, wend) DO UPDATE SET "
            "revision=excluded.revision, count=excluded.count, sum=excluded.sum, "
            "min=excluded.min, max=excluded.max, mean=excluded.mean",
            (rec["key"], rec["wstart"], rec["wend"], rec["revision"], rec["count"], rec["sum"],
             rec["min"], rec["max"], rec["mean"]))
