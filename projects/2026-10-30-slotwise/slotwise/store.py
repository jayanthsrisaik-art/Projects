"""SQLite persistence and the race-safe booking path.

Every write (book, cancel, reschedule) runs inside `BEGIN IMMEDIATE`, which
takes SQLite's single write lock before reading. The slot is re-validated with
the same `compute_slots` the read API uses, against the bookings visible inside
that transaction, so two concurrent requests for one slot serialise and the
second one sees the first one's row and gets `SlotUnavailable`.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from .availability import (
    EventType,
    compute_slots,
    event_type_from_dict,
    host_local_date,
    iso_z,
    parse_instant,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_types (
    slug   TEXT PRIMARY KEY,
    config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id              TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL REFERENCES event_types(slug),
    start_utc       TEXT NOT NULL,
    end_utc         TEXT NOT NULL,
    blocked_start   TEXT NOT NULL,
    blocked_end     TEXT NOT NULL,
    host_date       TEXT NOT NULL,
    invitee_name    TEXT NOT NULL,
    invitee_email   TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    token_hash      TEXT NOT NULL,
    rescheduled_to  TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookings_active
    ON bookings(event_type, status, blocked_start);
CREATE TABLE IF NOT EXISTS busy_blocks (
    id         INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL REFERENCES event_types(slug),
    start_utc  TEXT NOT NULL,
    end_utc    TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS idempotency (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    booking_id   TEXT NOT NULL,
    token        TEXT NOT NULL
);
"""


class NotFound(LookupError):
    pass


class SlotUnavailable(Exception):
    pass


class BadToken(PermissionError):
    pass


class Conflict(Exception):
    """Idempotency key reused with a different request, or invalid state change."""


@dataclass
class Booking:
    id: str
    event_type: str
    start: datetime
    end: datetime
    host_date: str
    invitee_name: str
    invitee_email: str
    status: str
    rescheduled_to: str | None
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "start": iso_z(self.start),
            "end": iso_z(self.end),
            "host_date": self.host_date,
            "invitee_name": self.invitee_name,
            "invitee_email": self.invitee_email,
            "status": self.status,
            "rescheduled_to": self.rescheduled_to,
            "created_at": self.created_at,
        }


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row_to_booking(r: sqlite3.Row) -> Booking:
    return Booking(
        id=r["id"],
        event_type=r["event_type"],
        start=parse_instant(r["start_utc"]),
        end=parse_instant(r["end_utc"]),
        host_date=r["host_date"],
        invitee_name=r["invitee_name"],
        invitee_email=r["invitee_email"],
        status=r["status"],
        rescheduled_to=r["rescheduled_to"],
        created_at=r["created_at"],
    )


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # One connection per operation keeps the store safe to share across
        # FastAPI's worker threads; isolation_level=None lets us issue
        # BEGIN IMMEDIATE ourselves.
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _write(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ event types

    def upsert_event_type(self, config: dict) -> EventType:
        et = event_type_from_dict(config)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO event_types(slug, config) VALUES (?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET config = excluded.config",
                (et.slug, json.dumps(config, sort_keys=True)),
            )
        return et

    def list_event_types(self) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute("SELECT config FROM event_types ORDER BY slug").fetchall()
        return [json.loads(r["config"]) for r in rows]

    def _event_type(self, conn: sqlite3.Connection, slug: str) -> EventType:
        row = conn.execute("SELECT config FROM event_types WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise NotFound(f"event type {slug!r} not found")
        return event_type_from_dict(json.loads(row["config"]))

    def get_event_type(self, slug: str) -> EventType:
        with self._read() as conn:
            return self._event_type(conn, slug)

    def add_busy_block(self, slug: str, start: datetime, end: datetime, label: str = "") -> None:
        if end <= start:
            raise ValueError("busy block must end after it starts")
        with self._write() as conn:
            self._event_type(conn, slug)
            conn.execute(
                "INSERT INTO busy_blocks(event_type, start_utc, end_utc, label) VALUES (?,?,?,?)",
                (slug, iso_z(start), iso_z(end), label),
            )

    # ------------------------------------------------------------ availability

    def _busy_and_counts(
        self,
        conn: sqlite3.Connection,
        slug: str,
        lo: datetime,
        hi: datetime,
        exclude_id: str | None = None,
    ) -> tuple[list[tuple[datetime, datetime]], dict[str, int]]:
        # ISO-8601 'Z' strings of equal width sort chronologically, so the
        # overlap test can run in SQL on the index.
        rows = conn.execute(
            "SELECT id, blocked_start, blocked_end FROM bookings "
            "WHERE event_type = ? AND status = 'confirmed' "
            "AND blocked_start < ? AND blocked_end > ?",
            (slug, iso_z(hi), iso_z(lo)),
        ).fetchall()
        busy = [
            (parse_instant(r["blocked_start"]), parse_instant(r["blocked_end"]))
            for r in rows
            if r["id"] != exclude_id
        ]
        blocks = conn.execute(
            "SELECT start_utc, end_utc FROM busy_blocks "
            "WHERE event_type = ? AND start_utc < ? AND end_utc > ?",
            (slug, iso_z(hi), iso_z(lo)),
        ).fetchall()
        busy += [(parse_instant(r["start_utc"]), parse_instant(r["end_utc"])) for r in blocks]
        # Daily-cap counts need whole host-local days, not just the overlap.
        counts: dict[str, int] = {}
        lo_day = iso_z(lo - timedelta(days=2))[:10]
        hi_day = iso_z(hi + timedelta(days=2))[:10]
        for r in conn.execute(
            "SELECT id, host_date FROM bookings WHERE event_type = ? AND status = 'confirmed' "
            "AND host_date BETWEEN ? AND ?",
            (slug, lo_day, hi_day),
        ):
            if r["id"] != exclude_id:
                counts[r["host_date"]] = counts.get(r["host_date"], 0) + 1
        return busy, counts

    def _slots(
        self,
        conn: sqlite3.Connection,
        et: EventType,
        start: datetime,
        end: datetime,
        now: datetime,
        exclude_id: str | None = None,
    ) -> list[datetime]:
        pad = timedelta(days=2, minutes=et.buffer_before_min + et.buffer_after_min + et.duration_min)
        busy, counts = self._busy_and_counts(conn, et.slug, start - pad, end + pad, exclude_id)
        return compute_slots(
            et, range_start=start, range_end=end, now=now, busy=busy, bookings_per_day=counts
        )

    def available_slots(self, slug: str, start: datetime, end: datetime, now: datetime) -> list[datetime]:
        with self._read() as conn:
            et = self._event_type(conn, slug)
            return self._slots(conn, et, start, end, now)

    def _assert_bookable(
        self, conn, et: EventType, start: datetime, now: datetime, exclude_id: str | None = None
    ) -> None:
        window = self._slots(conn, et, start, start + timedelta(seconds=1), now, exclude_id)
        if start not in window:
            raise SlotUnavailable(f"{iso_z(start)} is not an available slot for {et.slug}")

    # ------------------------------------------------------------ bookings

    def _insert_booking(
        self, conn, et: EventType, start: datetime, name: str, email: str, now: datetime
    ) -> tuple[Booking, str]:
        booking_id = uuid.uuid4().hex[:12]
        token = secrets.token_urlsafe(16)
        b0, b1 = et.blocked_interval(start)
        end = start + timedelta(minutes=et.duration_min)
        conn.execute(
            "INSERT INTO bookings(id, event_type, start_utc, end_utc, blocked_start, blocked_end, "
            "host_date, invitee_name, invitee_email, status, token_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'confirmed', ?, ?)",
            (
                booking_id, et.slug, iso_z(start), iso_z(end), iso_z(b0), iso_z(b1),
                host_local_date(et, start), name, email, _hash_token(token), iso_z(now),
            ),
        )
        row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return _row_to_booking(row), token

    def book(
        self,
        slug: str,
        start: datetime,
        name: str,
        email: str,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> tuple[Booking, str, bool]:
        """Book a slot. Returns (booking, cancel_token, replayed)."""
        request_hash = hashlib.sha256(
            json.dumps([slug, iso_z(start), name, email]).encode()
        ).hexdigest()
        with self._write() as conn:
            if idempotency_key:
                prior = conn.execute(
                    "SELECT * FROM idempotency WHERE key = ?", (idempotency_key,)
                ).fetchone()
                if prior is not None:
                    if prior["request_hash"] != request_hash:
                        raise Conflict("Idempotency-Key was already used for a different request")
                    row = conn.execute(
                        "SELECT * FROM bookings WHERE id = ?", (prior["booking_id"],)
                    ).fetchone()
                    return _row_to_booking(row), prior["token"], True
            et = self._event_type(conn, slug)
            self._assert_bookable(conn, et, start, now)
            booking, token = self._insert_booking(conn, et, start, name, email, now)
            if idempotency_key:
                # The raw token is kept only so a retried request can be answered
                # identically; production would expire these rows.
                conn.execute(
                    "INSERT INTO idempotency(key, request_hash, booking_id, token) VALUES (?,?,?,?)",
                    (idempotency_key, request_hash, booking.id, token),
                )
            return booking, token, False

    def get_booking(self, booking_id: str) -> Booking:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if row is None:
            raise NotFound(f"booking {booking_id!r} not found")
        return _row_to_booking(row)

    def _owned(self, conn, booking_id: str, token: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if row is None:
            raise NotFound(f"booking {booking_id!r} not found")
        if not secrets.compare_digest(row["token_hash"], _hash_token(token)):
            raise BadToken("invalid booking token")
        return row

    def cancel(self, booking_id: str, token: str) -> Booking:
        """Cancel a booking; cancelling twice is a no-op that returns the same state."""
        with self._write() as conn:
            row = self._owned(conn, booking_id, token)
            if row["status"] == "confirmed":
                conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            return _row_to_booking(row)

    def reschedule(
        self, booking_id: str, token: str, new_start: datetime, now: datetime
    ) -> tuple[Booking, str]:
        """Atomically move a booking: the new slot is checked as if the old one
        were already free, and either both changes commit or neither does."""
        with self._write() as conn:
            row = self._owned(conn, booking_id, token)
            if row["status"] != "confirmed":
                raise Conflict("only confirmed bookings can be rescheduled")
            et = self._event_type(conn, row["event_type"])
            self._assert_bookable(conn, et, new_start, now, exclude_id=booking_id)
            conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
            new, new_token = self._insert_booking(
                conn, et, new_start, row["invitee_name"], row["invitee_email"], now
            )
            conn.execute(
                "UPDATE bookings SET rescheduled_to = ? WHERE id = ?", (new.id, booking_id)
            )
            return new, new_token

    def list_bookings(self, slug: str | None = None) -> list[Booking]:
        with self._read() as conn:
            if slug:
                rows = conn.execute(
                    "SELECT * FROM bookings WHERE event_type = ? ORDER BY start_utc", (slug,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM bookings ORDER BY start_utc").fetchall()
        return [_row_to_booking(r) for r in rows]
