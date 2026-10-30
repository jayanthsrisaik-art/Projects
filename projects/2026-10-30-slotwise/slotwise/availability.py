"""Pure slot engine: turn weekly host-local availability into bookable UTC slots.

Everything here is a pure function of (event type, busy intervals, now, range),
so the booking path can re-run exactly the same computation inside its write
transaction and the API never offers a slot the writer would reject.

DST rules (host timezone):
  * A wall-clock time that does not exist (spring-forward gap) resolves to the
    transition instant, e.g. 02:30 on 2026-03-08 in New York -> 03:00 EDT.
  * An ambiguous wall-clock time (fall-back overlap) resolves to the *earlier*
    instant when it starts a window and the *later* instant when it ends one,
    so a window keeps every real minute the host could mean.
  * Slots are stepped in real (UTC) minutes from the resolved window start, so
    a 00:00-04:00 window holds 3h of slots on spring-forward day and 5h on
    fall-back day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class EventType:
    slug: str
    title: str
    host_tz: str
    duration_min: int
    weekly: dict[str, list[tuple[str, str]]]
    step_min: int | None = None
    buffer_before_min: int = 0
    buffer_after_min: int = 0
    min_notice_min: int = 0
    max_days_ahead: int = 60
    daily_cap: int | None = None
    # "YYYY-MM-DD" -> windows replacing the weekly rule that day ([] = day off)
    overrides: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @property
    def step(self) -> int:
        return self.step_min or self.duration_min

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.host_tz)

    def blocked_interval(self, start: datetime) -> tuple[datetime, datetime]:
        """The time a booking at `start` occupies, buffers included."""
        return (
            start - timedelta(minutes=self.buffer_before_min),
            start + timedelta(minutes=self.duration_min + self.buffer_after_min),
        )


class ConfigError(ValueError):
    pass


def parse_hhmm(text: str, allow_24: bool = False) -> time | None:
    """Parse "HH:MM". Returns None for "24:00" (end of day) when allowed."""
    try:
        hh, mm = text.split(":")
        h, m = int(hh), int(mm)
    except ValueError as exc:
        raise ConfigError(f"bad time {text!r}, expected HH:MM") from exc
    if allow_24 and (h, m) == (24, 0):
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ConfigError(f"bad time {text!r}")
    return time(h, m)


def event_type_from_dict(d: dict) -> EventType:
    try:
        ZoneInfo(d["host_tz"])
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError, KeyError
        raise ConfigError(f"unknown timezone {d.get('host_tz')!r}") from exc
    weekly = {k: [tuple(w) for w in v] for k, v in d.get("weekly", {}).items()}
    for key in weekly:
        if key not in WEEKDAYS:
            raise ConfigError(f"unknown weekday {key!r}")
    overrides = {k: [tuple(w) for w in v] for k, v in d.get("overrides", {}).items()}
    for key in overrides:
        date.fromisoformat(key)
    et = EventType(
        slug=d["slug"],
        title=d.get("title", d["slug"]),
        host_tz=d["host_tz"],
        duration_min=int(d["duration_min"]),
        weekly=weekly,
        step_min=d.get("step_min"),
        buffer_before_min=int(d.get("buffer_before_min", 0)),
        buffer_after_min=int(d.get("buffer_after_min", 0)),
        min_notice_min=int(d.get("min_notice_min", 0)),
        max_days_ahead=int(d.get("max_days_ahead", 60)),
        daily_cap=d.get("daily_cap"),
        overrides=overrides,
    )
    if et.duration_min <= 0 or et.step <= 0:
        raise ConfigError("duration_min and step_min must be positive")
    if min(et.buffer_before_min, et.buffer_after_min, et.min_notice_min) < 0:
        raise ConfigError("buffers and notice must be >= 0")
    for windows in [*weekly.values(), *overrides.values()]:
        for start, end in windows:
            s, e = parse_hhmm(start), parse_hhmm(end, allow_24=True)
            if e is not None and e <= s:
                raise ConfigError(f"window {start}-{end} must end after it starts")
    return et


# ---------------------------------------------------------------- DST helpers

def _wall(dt_utc: datetime, zone: ZoneInfo) -> datetime:
    return dt_utc.astimezone(zone).replace(tzinfo=None, fold=0)


def resolve_local(d: date, t: time | None, zone: ZoneInfo, *, is_end: bool) -> datetime:
    """Map a host-local wall time on date `d` to one UTC instant (see module doc).

    `t=None` means 24:00, i.e. midnight starting the next day.
    """
    if t is None:
        d, t = d + timedelta(days=1), time(0, 0)
    naive = datetime.combine(d, t)
    u0 = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    u1 = naive.replace(tzinfo=zone, fold=1).astimezone(UTC)
    exists0 = _wall(u0, zone) == naive
    exists1 = _wall(u1, zone) == naive
    if exists0 and exists1:
        if u0 == u1:
            return u0
        return max(u0, u1) if is_end else min(u0, u1)  # ambiguous (overlap)
    # Nonexistent (gap): the transition instant lies between the two readings.
    lo, hi = min(u0, u1), max(u0, u1)
    after = hi.astimezone(zone).utcoffset()
    while hi - lo > timedelta(minutes=1):  # bisect for the first post-gap minute
        mid = lo + (hi - lo) / 2
        mid = mid.replace(second=0, microsecond=0)
        if mid.astimezone(zone).utcoffset() == after:
            hi = mid
        else:
            lo = mid
    return hi


def windows_for_date(et: EventType, d: date) -> list[tuple[datetime, datetime]]:
    """Resolved UTC availability windows for host-local date `d`."""
    key = d.isoformat()
    raw = et.overrides[key] if key in et.overrides else et.weekly.get(WEEKDAYS[d.weekday()], [])
    zone = et.zone
    out = []
    for start, end in raw:
        ws = resolve_local(d, parse_hhmm(start), zone, is_end=False)
        we = resolve_local(d, parse_hhmm(end, allow_24=True), zone, is_end=True)
        if we > ws:
            out.append((ws, we))
    return out


# ---------------------------------------------------------------- slots

def _overlaps(a0: datetime, a1: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(a0 < b1 and b0 < a1 for b0, b1 in busy)


def compute_slots(
    et: EventType,
    *,
    range_start: datetime,
    range_end: datetime,
    now: datetime,
    busy: list[tuple[datetime, datetime]],
    bookings_per_day: dict[str, int] | None = None,
) -> list[datetime]:
    """UTC slot starts in [range_start, range_end) that a new booking may take.

    `busy` holds already-blocked UTC intervals (existing bookings including their
    buffers, plus external busy blocks). `bookings_per_day` counts confirmed
    bookings per host-local date for the daily cap.
    """
    for dt in (range_start, range_end, now):
        if dt.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
    zone = et.zone
    per_day = bookings_per_day or {}
    earliest = now + timedelta(minutes=et.min_notice_min)
    last_day = now.astimezone(zone).date() + timedelta(days=et.max_days_ahead)
    dur, step = timedelta(minutes=et.duration_min), timedelta(minutes=et.step)

    # Host-local dates that can contribute slots to the requested range; one
    # day of slack on each side covers windows that end at 24:00.
    d = range_start.astimezone(zone).date() - timedelta(days=1)
    stop = min(range_end.astimezone(zone).date() + timedelta(days=1), last_day)
    slots: list[datetime] = []
    while d <= stop:
        if et.daily_cap is not None and per_day.get(d.isoformat(), 0) >= et.daily_cap:
            d += timedelta(days=1)
            continue
        for ws, we in windows_for_date(et, d):
            s = ws
            while s + dur <= we:
                if (
                    range_start <= s < range_end
                    and s >= earliest
                    and s.astimezone(zone).date() <= last_day
                    and not _overlaps(*et.blocked_interval(s), busy)
                ):
                    slots.append(s)
                s += step
        d += timedelta(days=1)
    return sorted(set(slots))


def host_local_date(et: EventType, start: datetime) -> str:
    """Host-local date a slot belongs to (for the daily cap)."""
    return start.astimezone(et.zone).date().isoformat()


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(text: str) -> datetime:
    """Parse an ISO-8601 instant; naive input is rejected rather than guessed."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"instant {text!r} needs a UTC offset or 'Z'")
    return dt.astimezone(UTC)
