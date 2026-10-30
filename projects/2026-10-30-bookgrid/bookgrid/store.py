"""SQLite-backed booking store.

Every write that depends on a conflict check runs inside ``BEGIN IMMEDIATE``:
SQLite takes the database write lock *before* the check, so two concurrent
requests for the same slot are serialised and the second one sees the first
one's rows. A plain ``SELECT`` followed by ``INSERT`` in autocommit mode does
not have this property (see ``naive_book`` and ``python -m bookgrid race``).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .recurrence import Rule, RuleError, expand_dates, localize, parse_rrule, to_local

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    open_days TEXT NOT NULL,
    buffer_minutes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    title TEXT NOT NULL,
    organizer TEXT NOT NULL,
    start_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    rrule TEXT
);
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id),
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    recurrence_date TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    moved INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    UNIQUE (series_id, recurrence_date)
);
CREATE INDEX IF NOT EXISTS occ_by_resource ON occurrences(resource_id, status, start_ts);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response TEXT NOT NULL
);
"""

DAY_NAMES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class BookingError(Exception):
    """Raised for invalid input. ``status`` maps to an HTTP status code."""

    def __init__(self, message: str, status: int = 422, details: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details


def parse_hhmm(value: str, what: str) -> time:
    m = HHMM.match(value or "")
    if not m:
        raise BookingError(f"{what} must be HH:MM (24h), got {value!r}")
    return time(int(m.group(1)), int(m.group(2)))


def parse_day(value: str, what: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise BookingError(f"{what} must be YYYY-MM-DD, got {value!r}") from None


@dataclass(frozen=True)
class Resource:
    id: int
    name: str
    timezone: str
    open_time: str
    close_time: str
    open_days: tuple[int, ...]
    buffer_minutes: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "timezone": self.timezone,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open_days": [DAY_NAMES[d] for d in self.open_days],
            "buffer_minutes": self.buffer_minutes,
        }


@dataclass
class BookingRequest:
    resource_id: int
    title: str
    organizer: str
    date: str
    start_time: str
    duration_minutes: int
    rrule: str | None = None
    on_conflict: str = "reject"  # "reject" | "skip"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BookingRequest":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PlannedOccurrence:
    recurrence_date: date
    start_ts: int
    end_ts: int
    note: str | None
    problems: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _iso_local(ts: int, tz: ZoneInfo) -> str:
    return to_local(ts, tz).isoformat(timespec="minutes")


def request_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        self.path = path
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    # -- connections -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """A transaction holding SQLite's write lock from its first statement."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- resources ---------------------------------------------------------
    def create_resource(
        self,
        name: str,
        timezone_name: str,
        open_time: str = "08:00",
        close_time: str = "18:00",
        open_days: list[str] | None = None,
        buffer_minutes: int = 0,
    ) -> Resource:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise BookingError(f"unknown timezone {timezone_name!r}") from None
        if parse_hhmm(open_time, "open_time") >= parse_hhmm(close_time, "close_time"):
            raise BookingError("open_time must be before close_time")
        codes = open_days or ["MO", "TU", "WE", "TH", "FR"]
        if not codes or any(c.upper() not in DAY_NAMES for c in codes):
            raise BookingError("open_days accepts MO,TU,WE,TH,FR,SA,SU")
        days = sorted({DAY_NAMES.index(c.upper()) for c in codes})
        if not 0 <= buffer_minutes <= 120:
            raise BookingError("buffer_minutes must be between 0 and 120")
        with self.write_tx() as conn:
            if conn.execute("SELECT 1 FROM resources WHERE name = ?", (name,)).fetchone():
                raise BookingError(f"resource {name!r} already exists", status=409)
            cur = conn.execute(
                "INSERT INTO resources (name, timezone, open_time, close_time, open_days,"
                " buffer_minutes) VALUES (?, ?, ?, ?, ?, ?)",
                (name, timezone_name, open_time, close_time,
                 ",".join(str(d) for d in days), buffer_minutes),
            )
            rid = cur.lastrowid
        return self.get_resource(rid)

    def _resource_from_row(self, row: sqlite3.Row) -> Resource:
        return Resource(
            id=row["id"],
            name=row["name"],
            timezone=row["timezone"],
            open_time=row["open_time"],
            close_time=row["close_time"],
            open_days=tuple(int(x) for x in row["open_days"].split(",")),
            buffer_minutes=row["buffer_minutes"],
        )

    def get_resource(self, rid: int, conn: sqlite3.Connection | None = None) -> Resource:
        own = conn is None
        conn = conn or self.connect()
        try:
            row = conn.execute("SELECT * FROM resources WHERE id = ?", (rid,)).fetchone()
        finally:
            if own:
                conn.close()
        if row is None:
            raise BookingError(f"resource {rid} not found", status=404)
        return self._resource_from_row(row)

    def list_resources(self) -> list[Resource]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM resources ORDER BY id").fetchall()
        finally:
            conn.close()
        return [self._resource_from_row(r) for r in rows]

    # -- planning ----------------------------------------------------------
    def _window_problems(self, res: Resource, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        tz = res.tz
        ls, le = to_local(start_ts, tz), to_local(end_ts, tz)
        problems = []
        if ls.weekday() not in res.open_days:
            problems.append({"kind": "closed_day", "message": f"{res.name} is closed on {ls:%A}s"})
        open_t, close_t = parse_hhmm(res.open_time, "open"), parse_hhmm(res.close_time, "close")
        if ls.time() < open_t or le.date() != ls.date() or le.time() > close_t:
            problems.append({
                "kind": "outside_hours",
                "message": f"{ls:%H:%M}-{le:%H:%M} is outside opening hours "
                           f"{res.open_time}-{res.close_time}",
            })
        return problems

    def _conflicts(
        self,
        conn: sqlite3.Connection,
        res: Resource,
        start_ts: int,
        end_ts: int,
        exclude_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Active occurrences that overlap [start, end) once buffers are applied.

        The buffer is turnaround time after every booking, so two bookings
        clash when ``a.start < b.end + buffer`` and ``b.start < a.end + buffer``.
        """
        buf = res.buffer_minutes * 60
        rows = conn.execute(
            "SELECT o.id, o.series_id, o.start_ts, o.end_ts, s.title, s.organizer"
            " FROM occurrences o JOIN series s ON s.id = o.series_id"
            " WHERE o.resource_id = ? AND o.status = 'active'"
            "   AND o.start_ts < ? AND o.end_ts + ? > ? AND o.id IS NOT ?"
            " ORDER BY o.start_ts",
            (res.id, end_ts + buf, buf, start_ts, exclude_id),
        ).fetchall()
        tz = res.tz
        return [
            {
                "occurrence_id": r["id"],
                "series_id": r["series_id"],
                "title": r["title"],
                "organizer": r["organizer"],
                "local_start": _iso_local(r["start_ts"], tz),
                "local_end": _iso_local(r["end_ts"], tz),
            }
            for r in rows
        ]

    def _validate(self, req: BookingRequest) -> tuple[date, time, Rule | None]:
        if not isinstance(req.title, str) or not req.title.strip():
            raise BookingError("title is required")
        if not isinstance(req.organizer, str) or not req.organizer.strip():
            raise BookingError("organizer is required")
        if req.on_conflict not in ("reject", "skip"):
            raise BookingError("on_conflict must be 'reject' or 'skip'")
        if not isinstance(req.duration_minutes, int) or not 5 <= req.duration_minutes <= 720:
            raise BookingError("duration_minutes must be an integer between 5 and 720")
        rule = None
        if req.rrule:
            try:
                rule = parse_rrule(req.rrule)
            except RuleError as exc:
                raise BookingError(f"rrule: {exc}") from None
        return parse_day(req.date), parse_hhmm(req.start_time, "start_time"), rule

    def plan(self, conn: sqlite3.Connection, req: BookingRequest) -> tuple[Resource, Rule | None, list[PlannedOccurrence]]:
        day, wall, rule = self._validate(req)
        res = self.get_resource(req.resource_id, conn)
        try:
            dates = expand_dates(day, rule)
        except RuleError as exc:
            raise BookingError(f"rrule: {exc}") from None
        if not dates:
            raise BookingError("rule produces no occurrences")
        planned: list[PlannedOccurrence] = []
        prev_end_with_buf: int | None = None
        for d in dates:
            inst = localize(d, wall, res.tz)
            start = _ts(inst.utc)
            end = start + req.duration_minutes * 60
            occ = PlannedOccurrence(d, start, end, inst.note)
            occ.problems.extend(self._window_problems(res, start, end))
            clashes = self._conflicts(conn, res, start, end)
            if clashes:
                occ.problems.append({"kind": "conflict", "message": "overlaps an existing booking",
                                     "with": clashes})
            if prev_end_with_buf is not None and start < prev_end_with_buf:
                occ.problems.append({"kind": "self_overlap",
                                     "message": "overlaps the previous occurrence of this series"})
            prev_end_with_buf = end + res.buffer_minutes * 60
            planned.append(occ)
        return res, rule, planned

    def _describe(self, res: Resource, occ: PlannedOccurrence) -> dict[str, Any]:
        out = {
            "recurrence_date": occ.recurrence_date.isoformat(),
            "local_start": _iso_local(occ.start_ts, res.tz),
            "local_end": _iso_local(occ.end_ts, res.tz),
            "start_utc": datetime.fromtimestamp(occ.start_ts, timezone.utc).isoformat(),
            "note": occ.note,
        }
        if occ.problems:
            out["problems"] = occ.problems
        return out

    # -- bookings ----------------------------------------------------------
    def preview(self, req: BookingRequest) -> dict[str, Any]:
        conn = self.connect()
        try:
            res, rule, planned = self.plan(conn, req)
        finally:
            conn.close()
        return {
            "resource": res.name,
            "rrule": rule.to_string() if rule else None,
            "occurrences": [self._describe(res, o) for o in planned],
            "bookable": sum(o.ok for o in planned),
            "blocked": sum(not o.ok for o in planned),
        }

    def book(self, req: BookingRequest, idempotency_key: str | None = None,
             body_for_hash: dict[str, Any] | None = None) -> tuple[int, dict[str, Any], bool]:
        """Create a series atomically. Returns (status, body, replayed)."""
        digest = request_hash(body_for_hash if body_for_hash is not None else req.__dict__)
        with self.write_tx() as conn:
            if idempotency_key:
                row = conn.execute("SELECT * FROM idempotency WHERE key = ?",
                                   (idempotency_key,)).fetchone()
                if row:
                    if row["request_hash"] != digest:
                        raise BookingError("Idempotency-Key was already used with a different body",
                                           status=422)
                    return row["status_code"], json.loads(row["response"]), True
            res, rule, planned = self.plan(conn, req)
            blocked = [o for o in planned if not o.ok]
            if blocked and (req.on_conflict == "reject" or len(blocked) == len(planned)):
                # Nothing is written; failures are deliberately not cached
                # under the idempotency key so the client can retry later.
                raise BookingError(
                    f"{len(blocked)} of {len(planned)} occurrence(s) cannot be booked",
                    status=409,
                    details=[self._describe(res, o) for o in blocked],
                )
            cur = conn.execute(
                "INSERT INTO series (resource_id, title, organizer, start_date, start_time,"
                " duration_minutes, rrule) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (res.id, req.title.strip(), req.organizer.strip(), req.date, req.start_time,
                 req.duration_minutes, rule.to_string() if rule else None),
            )
            sid = cur.lastrowid
            booked = []
            for o in planned:
                if not o.ok:
                    continue
                c = conn.execute(
                    "INSERT INTO occurrences (series_id, resource_id, recurrence_date, start_ts,"
                    " end_ts, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, res.id, o.recurrence_date.isoformat(), o.start_ts, o.end_ts, o.note),
                )
                booked.append({"id": c.lastrowid, **self._describe(res, o)})
            body = {
                "series_id": sid,
                "resource": res.name,
                "rrule": rule.to_string() if rule else None,
                "booked": booked,
                "skipped": [self._describe(res, o) for o in blocked],
            }
            if idempotency_key:
                conn.execute(
                    "INSERT INTO idempotency (key, request_hash, status_code, response)"
                    " VALUES (?, ?, ?, ?)",
                    (idempotency_key, digest, 201, json.dumps(body)),
                )
        return 201, body, False

    def _occurrence_row(self, conn: sqlite3.Connection, oid: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM occurrences WHERE id = ?", (oid,)).fetchone()
        if row is None:
            raise BookingError(f"occurrence {oid} not found", status=404)
        return row

    def cancel(self, oid: int, scope: str = "this") -> dict[str, Any]:
        if scope not in ("this", "following", "series"):
            raise BookingError("scope must be 'this', 'following' or 'series'")
        with self.write_tx() as conn:
            row = self._occurrence_row(conn, oid)
            if scope == "this":
                cur = conn.execute(
                    "UPDATE occurrences SET status = 'cancelled' WHERE id = ? AND status = 'active'",
                    (oid,))
            elif scope == "following":
                cur = conn.execute(
                    "UPDATE occurrences SET status = 'cancelled' WHERE series_id = ?"
                    " AND recurrence_date >= ? AND status = 'active'",
                    (row["series_id"], row["recurrence_date"]))
            else:
                cur = conn.execute(
                    "UPDATE occurrences SET status = 'cancelled' WHERE series_id = ?"
                    " AND status = 'active'", (row["series_id"],))
        return {"series_id": row["series_id"], "scope": scope, "cancelled": cur.rowcount}

    def move(self, oid: int, new_date: str, new_time: str, duration_minutes: int | None = None) -> dict[str, Any]:
        """Reschedule one occurrence (it becomes an exception of its series)."""
        day = parse_day(new_date)
        wall = parse_hhmm(new_time, "start_time")
        with self.write_tx() as conn:
            row = self._occurrence_row(conn, oid)
            if row["status"] != "active":
                raise BookingError("cannot move a cancelled occurrence", status=409)
            res = self.get_resource(row["resource_id"], conn)
            dur = duration_minutes if duration_minutes is not None else (row["end_ts"] - row["start_ts"]) // 60
            if not isinstance(dur, int) or not 5 <= dur <= 720:
                raise BookingError("duration_minutes must be an integer between 5 and 720")
            inst = localize(day, wall, res.tz)
            start = _ts(inst.utc)
            end = start + dur * 60
            problems = self._window_problems(res, start, end)
            clashes = self._conflicts(conn, res, start, end, exclude_id=oid)
            if clashes:
                problems.append({"kind": "conflict", "message": "overlaps an existing booking",
                                 "with": clashes})
            if problems:
                raise BookingError("occurrence cannot be moved there", status=409, details=problems)
            conn.execute(
                "UPDATE occurrences SET start_ts = ?, end_ts = ?, moved = 1, note = ? WHERE id = ?",
                (start, end, inst.note, oid))
        return self.get_occurrence(oid)

    # -- queries -----------------------------------------------------------
    def _occ_dict(self, row: sqlite3.Row, tz: ZoneInfo) -> dict[str, Any]:
        return {
            "id": row["id"],
            "series_id": row["series_id"],
            "title": row["title"],
            "organizer": row["organizer"],
            "rrule": row["rrule"],
            "recurrence_date": row["recurrence_date"],
            "local_start": _iso_local(row["start_ts"], tz),
            "local_end": _iso_local(row["end_ts"], tz),
            "start_utc": datetime.fromtimestamp(row["start_ts"], timezone.utc).isoformat(),
            "status": row["status"],
            "moved": bool(row["moved"]),
            "note": row["note"],
        }

    _OCC_SELECT = (
        "SELECT o.*, s.title, s.organizer, s.rrule FROM occurrences o"
        " JOIN series s ON s.id = o.series_id"
    )

    def get_occurrence(self, oid: int) -> dict[str, Any]:
        conn = self.connect()
        try:
            row = conn.execute(self._OCC_SELECT + " WHERE o.id = ?", (oid,)).fetchone()
            if row is None:
                raise BookingError(f"occurrence {oid} not found", status=404)
            res = self.get_resource(row["resource_id"], conn)
        finally:
            conn.close()
        return self._occ_dict(row, res.tz)

    def get_series(self, sid: int) -> dict[str, Any]:
        conn = self.connect()
        try:
            s = conn.execute("SELECT * FROM series WHERE id = ?", (sid,)).fetchone()
            if s is None:
                raise BookingError(f"series {sid} not found", status=404)
            res = self.get_resource(s["resource_id"], conn)
            rows = conn.execute(self._OCC_SELECT + " WHERE o.series_id = ? ORDER BY o.recurrence_date",
                                (sid,)).fetchall()
        finally:
            conn.close()
        return {**dict(s), "resource": res.name,
                "occurrences": [self._occ_dict(r, res.tz) for r in rows]}

    def _local_day_bounds(self, res: Resource, first: date, last: date) -> tuple[int, int]:
        tz = res.tz
        lo = datetime.combine(first, time(0), tzinfo=tz)
        hi = datetime.combine(last + timedelta(days=1), time(0), tzinfo=tz)
        return _ts(lo), _ts(hi)

    def list_occurrences(self, rid: int, first: str, last: str,
                         include_cancelled: bool = False) -> list[dict[str, Any]]:
        d1, d2 = parse_day(first, "start"), parse_day(last, "end")
        if d2 < d1:
            raise BookingError("end must not be before start")
        if (d2 - d1).days > 62:
            raise BookingError("range is limited to 62 days")
        conn = self.connect()
        try:
            res = self.get_resource(rid, conn)
            lo, hi = self._local_day_bounds(res, d1, d2)
            sql = self._OCC_SELECT + " WHERE o.resource_id = ? AND o.start_ts < ? AND o.end_ts > ?"
            if not include_cancelled:
                sql += " AND o.status = 'active'"
            rows = conn.execute(sql + " ORDER BY o.start_ts", (rid, hi, lo)).fetchall()
        finally:
            conn.close()
        return [self._occ_dict(r, res.tz) for r in rows]

    def free_slots(self, rid: int, day: str, duration_minutes: int, step_minutes: int = 15) -> dict[str, Any]:
        """Start times on ``day`` where a booking of this length would be accepted."""
        d = parse_day(day)
        if not 5 <= duration_minutes <= 720:
            raise BookingError("duration must be between 5 and 720 minutes")
        if step_minutes not in (5, 10, 15, 20, 30, 60):
            raise BookingError("step must be one of 5, 10, 15, 20, 30, 60")
        conn = self.connect()
        try:
            res = self.get_resource(rid, conn)
            tz = res.tz
            if d.weekday() not in res.open_days:
                return {"resource": res.name, "date": day, "slots": [], "closed": True}
            ws = _ts(localize(d, parse_hhmm(res.open_time, "open"), tz).utc)
            we = _ts(localize(d, parse_hhmm(res.close_time, "close"), tz).utc)
            buf = res.buffer_minutes * 60
            busy = [
                (r["start_ts"], r["end_ts"])
                for r in conn.execute(
                    "SELECT start_ts, end_ts FROM occurrences WHERE resource_id = ?"
                    " AND status = 'active' AND start_ts < ? AND end_ts + ? > ?",
                    (rid, we + buf, buf, ws),
                )
            ]
        finally:
            conn.close()
        dur, step = duration_minutes * 60, step_minutes * 60
        slots = []
        s = ws
        while s + dur <= we:
            e = s + dur
            if not any(bs < e + buf and s < be + buf for bs, be in busy):
                slots.append({"local_start": _iso_local(s, tz), "local_end": _iso_local(e, tz)})
            s += step
        return {"resource": res.name, "date": day, "slots": slots, "closed": False}


def naive_book(store: Store, req: BookingRequest, pause: float = 0.0) -> bool:
    """Baseline: check-then-insert without a write lock. Used only by the race benchmark.

    Returns True if it inserted. ``pause`` widens the race window the way a
    slow network hop or GC pause would in production.
    """
    conn = store.connect()
    try:
        res, rule, planned = store.plan(conn, req)  # autocommit SELECTs, no lock held
        if any(not o.ok for o in planned):
            return False
        if pause:
            _time.sleep(pause)
        cur = conn.execute(
            "INSERT INTO series (resource_id, title, organizer, start_date, start_time,"
            " duration_minutes, rrule) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (res.id, req.title, req.organizer, req.date, req.start_time, req.duration_minutes, None),
        )
        for o in planned:
            conn.execute(
                "INSERT INTO occurrences (series_id, resource_id, recurrence_date, start_ts, end_ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, res.id, o.recurrence_date.isoformat(), o.start_ts, o.end_ts),
            )
        return True
    finally:
        conn.close()
