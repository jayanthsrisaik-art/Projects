"""SQLite persistence with optimistic concurrency and an append-only audit log.

Every write is a compare-and-swap on ``version`` inside ``BEGIN IMMEDIATE``, so
two editors who loaded the same version cannot both win, even across processes.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .diff import json_diff
from .schema import semantic_errors

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    key        TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    doc        TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_key TEXT NOT NULL,
    version  INTEGER NOT NULL,
    action   TEXT NOT NULL,
    actor    TEXT NOT NULL,
    comment  TEXT NOT NULL,
    at       TEXT NOT NULL,
    diff     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_flag ON audit(flag_key, id);
"""


class NotFound(Exception):
    pass


class Conflict(Exception):
    """Stale version: someone else wrote first."""

    def __init__(self, current_version: int | None):
        super().__init__(f"stale version; current is {current_version}")
        self.current_version = current_version


class Invalid(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class InUse(Exception):
    def __init__(self, dependents: list[str]):
        super().__init__(f"flag is a prerequisite of {dependents}")
        self.dependents = dependents


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        return {"version": r["version"], "updated_at": r["updated_at"], "flag": json.loads(r["doc"])}

    def list(self) -> list[dict]:
        with self._conn() as c:
            return [self._row(r) for r in c.execute("SELECT * FROM flags ORDER BY key")]

    def get(self, key: str) -> dict:
        with self._conn() as c:
            r = c.execute("SELECT * FROM flags WHERE key = ?", (key,)).fetchone()
        if r is None:
            raise NotFound(key)
        return self._row(r)

    def docs(self) -> dict[str, dict]:
        return {row["flag"]["key"]: row["flag"] for row in self.list()}

    def audit(self, key: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM audit WHERE flag_key = ? ORDER BY id DESC", (key,)).fetchall()
        return [{**dict(r), "diff": json.loads(r["diff"])} for r in rows]

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _docs_in(c: sqlite3.Connection) -> dict[str, dict]:
        return {r["key"]: json.loads(r["doc"]) for r in c.execute("SELECT key, doc FROM flags")}

    @staticmethod
    def _log(c, key, version, action, actor, comment, before, after) -> None:
        c.execute(
            "INSERT INTO audit(flag_key, version, action, actor, comment, at, diff) VALUES (?,?,?,?,?,?,?)",
            (key, version, action, actor, comment, _now(), json.dumps(json_diff(before, after))),
        )

    def create(self, doc: dict, actor: str, comment: str = "") -> dict:
        with self._tx() as c:
            others = self._docs_in(c)
            if doc["key"] in others:
                raise Conflict(None)
            errors = semantic_errors(doc, others)
            if errors:
                raise Invalid(errors)
            c.execute("INSERT INTO flags(key, version, doc, updated_at) VALUES (?, 1, ?, ?)",
                      (doc["key"], json.dumps(doc), _now()))
            self._log(c, doc["key"], 1, "create", actor, comment, {}, doc)
        return self.get(doc["key"])

    def update(self, key: str, doc: dict, expected_version: int, actor: str, comment: str = "",
               action: str = "update") -> dict:
        if doc["key"] != key:
            raise Invalid(["flag key cannot be changed"])
        with self._tx() as c:
            r = c.execute("SELECT * FROM flags WHERE key = ?", (key,)).fetchone()
            if r is None:
                raise NotFound(key)
            if r["version"] != expected_version:
                raise Conflict(r["version"])
            before = json.loads(r["doc"])
            others = self._docs_in(c)
            others.pop(key)
            errors = semantic_errors(doc, others)
            if errors:
                raise Invalid(errors)
            if not json_diff(before, doc):
                return {**self._row(r), "unchanged": True}  # no-op saves don't bump the version
            new_version = expected_version + 1
            cur = c.execute("UPDATE flags SET version = ?, doc = ?, updated_at = ? WHERE key = ? AND version = ?",
                            (new_version, json.dumps(doc), _now(), key, expected_version))
            if cur.rowcount != 1:  # defensive: BEGIN IMMEDIATE already serialises writers
                raise Conflict(None)
            self._log(c, key, new_version, action, actor, comment, before, doc)
        return self.get(key)

    def set_on(self, key: str, env: str, on: bool, expected_version: int, actor: str, comment: str = "") -> dict:
        current = self.get(key)
        doc = current["flag"]
        if env not in doc["environments"]:
            raise Invalid([f"unknown environment '{env}'"])
        doc["environments"][env]["on"] = on
        return self.update(key, doc, expected_version, actor, comment, action="toggle")

    def delete(self, key: str, expected_version: int, actor: str, comment: str = "") -> None:
        with self._tx() as c:
            r = c.execute("SELECT version, doc FROM flags WHERE key = ?", (key,)).fetchone()
            if r is None:
                raise NotFound(key)
            if r["version"] != expected_version:
                raise Conflict(r["version"])
            dependents = sorted(k for k, d in self._docs_in(c).items()
                                if any(p["key"] == key for p in d.get("prerequisites", [])) and k != key)
            if dependents:
                raise InUse(dependents)
            c.execute("DELETE FROM flags WHERE key = ?", (key,))
            self._log(c, key, expected_version + 1, "delete", actor, comment, json.loads(r["doc"]), {})
