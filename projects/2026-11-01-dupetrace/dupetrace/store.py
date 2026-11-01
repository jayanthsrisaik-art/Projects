"""SQLite storage and idempotent JSONL ingestion.

Reports are validated, parsed once at ingest time (exception, normalised message,
frames) and stored with the derived columns, so retrieval never re-parses text.
Re-ingesting the same file is a no-op; a known id arriving with *different*
content is counted as a conflict and the stored row is kept (first write wins).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .normalize import parse_report_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    component    TEXT NOT NULL,
    version      TEXT,
    content_hash TEXT NOT NULL,
    exc_type     TEXT,
    norm_message TEXT NOT NULL,
    frames_json  TEXT NOT NULL,
    ingested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at, id);
CREATE TABLE IF NOT EXISTS duplicate_links (
    report_id    TEXT NOT NULL REFERENCES reports(id),
    duplicate_of TEXT NOT NULL REFERENCES reports(id),
    source       TEXT NOT NULL,           -- 'triager' | 'import'
    created_at   TEXT NOT NULL,
    PRIMARY KEY (report_id, duplicate_of)
);
CREATE TABLE IF NOT EXISTS quarantine (
    line_no  INTEGER,
    raw      TEXT,
    reason   TEXT NOT NULL,
    seen_at  TEXT NOT NULL
);
"""

REQUIRED = ("id", "created_at", "title", "body")


@dataclass
class Report:
    id: str
    created_at: str
    title: str
    body: str
    component: str = "unknown"
    version: str | None = None
    exc_type: str | None = None
    norm_message: str = ""
    frames: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ts(self) -> datetime:
        return parse_ts(self.created_at)

    @property
    def has_trace(self) -> bool:
        return bool(self.frames)

    @property
    def top_frame(self) -> str | None:
        return self.frames[-1] if self.frames else None


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    return dt.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def validate(rec: object) -> str | None:
    """Return a rejection reason, or None if the record is acceptable."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    for k in REQUIRED:
        if not isinstance(rec.get(k), str) or not rec[k].strip():
            return f"missing or empty field: {k}"
    try:
        parse_ts(rec["created_at"])
    except ValueError as e:
        return f"bad created_at: {e}"
    if len(rec["body"]) > 100_000:
        return "body too large"
    return None


def build_report(rec: dict) -> Report:
    parsed = parse_report_text(rec["title"] + "\n" + rec["body"])
    return Report(
        id=rec["id"].strip(),
        created_at=parse_ts(rec["created_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        title=rec["title"].strip(),
        body=rec["body"],
        component=(rec.get("component") or "unknown").strip().lower() or "unknown",
        version=rec.get("version"),
        exc_type=parsed.exc_type,
        norm_message=parsed.message,
        frames=parsed.frames,
    )


def _hash(rec: dict) -> str:
    keys = ("created_at", "title", "body", "component", "version")
    return hashlib.sha256(json.dumps([rec.get(k) for k in keys]).encode()).hexdigest()


@dataclass
class IngestStats:
    inserted: int = 0
    unchanged: int = 0
    conflicts: int = 0
    quarantined: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def insert_report(conn: sqlite3.Connection, rec: dict) -> tuple[str, Report | None]:
    """Insert one validated record. Returns (status, report) with status in inserted/unchanged/conflict."""
    rep = build_report(rec)
    h = _hash(rec)
    row = conn.execute("SELECT content_hash FROM reports WHERE id = ?", (rep.id,)).fetchone()
    if row is not None:
        return ("unchanged" if row["content_hash"] == h else "conflict"), None
    conn.execute(
        "INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rep.id, rep.created_at, rep.title, rep.body, rep.component, rep.version, h,
         rep.exc_type, rep.norm_message, json.dumps(list(rep.frames)), _now()),
    )
    return "inserted", rep


def ingest_lines(conn: sqlite3.Connection, lines: Iterable[str]) -> IngestStats:
    stats = IngestStats()
    with conn:
        for i, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                reason = f"invalid JSON: {e.msg}"
                rec = None
            else:
                reason = validate(rec)
            if reason:
                conn.execute("INSERT INTO quarantine VALUES (?,?,?,?)", (i, line[:2000], reason, _now()))
                stats.quarantined += 1
                continue
            status, _ = insert_report(conn, rec)
            if status == "inserted":
                stats.inserted += 1
            elif status == "unchanged":
                stats.unchanged += 1
            else:
                stats.conflicts += 1
    return stats


def ingest_file(conn: sqlite3.Connection, path: str | Path) -> IngestStats:
    with open(path, encoding="utf-8") as f:
        return ingest_lines(conn, f)


def _row_to_report(row: sqlite3.Row) -> Report:
    return Report(
        id=row["id"], created_at=row["created_at"], title=row["title"], body=row["body"],
        component=row["component"], version=row["version"], exc_type=row["exc_type"],
        norm_message=row["norm_message"], frames=tuple(json.loads(row["frames_json"])),
    )


def load_reports(conn: sqlite3.Connection) -> list[Report]:
    """All reports in arrival order (created_at, id)."""
    return [_row_to_report(r) for r in conn.execute("SELECT * FROM reports ORDER BY created_at, id")]


def get_report(conn: sqlite3.Connection, report_id: str) -> Report | None:
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _row_to_report(row) if row else None


def link_duplicate(conn: sqlite3.Connection, report_id: str, duplicate_of: str, source: str = "triager") -> None:
    if report_id == duplicate_of:
        raise ValueError("a report cannot duplicate itself")
    with conn:
        conn.execute("INSERT OR IGNORE INTO duplicate_links VALUES (?,?,?,?)",
                     (report_id, duplicate_of, source, _now()))


def duplicate_links(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute("SELECT report_id, duplicate_of FROM duplicate_links ORDER BY 1, 2")]
