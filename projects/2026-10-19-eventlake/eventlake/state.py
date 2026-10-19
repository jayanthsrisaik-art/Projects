"""SQLite-backed ingestion state: checkpoints, seen keys, watermark."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint (path TEXT PRIMARY KEY, line_no INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, partition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class State:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def checkpoint(self, path: str) -> int:
        row = self.db.execute("SELECT line_no FROM checkpoint WHERE path=?", (path,)).fetchone()
        return row[0] if row else 0

    def set_checkpoint(self, path: str, line_no: int) -> None:
        self.db.execute("INSERT INTO checkpoint VALUES (?,?) ON CONFLICT(path) "
                        "DO UPDATE SET line_no=excluded.line_no", (path, line_no))

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark_seen(self, key: str, partition: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, partition))

    def get_meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
