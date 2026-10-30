"""A small, strict subset of RFC 5545 RRULE and wall-clock localisation.

Supported: FREQ=DAILY|WEEKLY, INTERVAL, BYDAY (weekly only), COUNT or UNTIL.
Every series must be bounded (COUNT or UNTIL) because occurrences are
materialised as rows; an unbounded rule cannot be conflict-checked up front.

Occurrences are generated as *local dates*. The wall-clock time is attached
per date in the resource's timezone, so "09:00 every Monday" stays 09:00
local across DST changes while its UTC instant moves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MAX_OCCURRENCES = 366
DAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    freq: str  # "DAILY" | "WEEKLY"
    interval: int = 1
    byday: tuple[int, ...] = ()  # weekday numbers, Monday=0
    count: int | None = None
    until: date | None = None

    def to_string(self) -> str:
        parts = [f"FREQ={self.freq}"]
        if self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.byday:
            parts.append("BYDAY=" + ",".join(DAY_CODES[d] for d in self.byday))
        if self.count is not None:
            parts.append(f"COUNT={self.count}")
        if self.until is not None:
            parts.append(f"UNTIL={self.until.strftime('%Y%m%d')}")
        return ";".join(parts)


def parse_rrule(text: str) -> Rule:
    """Parse e.g. ``FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=6``."""
    if not text or not text.strip():
        raise RuleError("empty rule")
    fields: dict[str, str] = {}
    for part in text.strip().removeprefix("RRULE:").split(";"):
        if "=" not in part:
            raise RuleError(f"malformed part {part!r}")
        key, value = part.split("=", 1)
        key = key.strip().upper()
        if key in fields:
            raise RuleError(f"duplicate {key}")
        fields[key] = value.strip().upper()

    unknown = set(fields) - {"FREQ", "INTERVAL", "BYDAY", "COUNT", "UNTIL"}
    if unknown:
        raise RuleError(f"unsupported parts: {', '.join(sorted(unknown))}")

    freq = fields.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        raise RuleError("FREQ must be DAILY or WEEKLY")

    try:
        interval = int(fields.get("INTERVAL", "1"))
    except ValueError:
        raise RuleError("INTERVAL must be an integer") from None
    if not 1 <= interval <= 52:
        raise RuleError("INTERVAL must be between 1 and 52")

    byday: tuple[int, ...] = ()
    if "BYDAY" in fields:
        if freq != "WEEKLY":
            raise RuleError("BYDAY is only supported with FREQ=WEEKLY")
        codes = fields["BYDAY"].split(",")
        if any(c not in DAY_CODES for c in codes):
            raise RuleError("BYDAY accepts MO,TU,WE,TH,FR,SA,SU")
        byday = tuple(sorted({DAY_CODES.index(c) for c in codes}))

    count = until = None
    if "COUNT" in fields and "UNTIL" in fields:
        raise RuleError("COUNT and UNTIL are mutually exclusive")
    if "COUNT" in fields:
        try:
            count = int(fields["COUNT"])
        except ValueError:
            raise RuleError("COUNT must be an integer") from None
        if not 1 <= count <= MAX_OCCURRENCES:
            raise RuleError(f"COUNT must be between 1 and {MAX_OCCURRENCES}")
    elif "UNTIL" in fields:
        raw = fields["UNTIL"][:8]  # accept 20261231 or 20261231T235959Z
        try:
            until = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            raise RuleError("UNTIL must look like YYYYMMDD") from None
    else:
        raise RuleError("series must be bounded with COUNT or UNTIL")

    return Rule(freq=freq, interval=interval, byday=byday, count=count, until=until)


def expand_dates(start: date, rule: Rule | None) -> list[date]:
    """Local dates of every occurrence, in order.

    Like python-dateutil (and unlike strict RFC 5545), ``start`` is only an
    occurrence if it matches the rule; it anchors the INTERVAL phase.
    """
    if rule is None:
        return [start]
    out: list[date] = []

    def done(d: date) -> bool:
        return (rule.until is not None and d > rule.until) or (
            rule.count is not None and len(out) >= rule.count
        )

    if rule.freq == "DAILY":
        d = start
        while not done(d):
            out.append(d)
            d += timedelta(days=rule.interval)
            if len(out) > MAX_OCCURRENCES:
                break
    else:
        days = rule.byday or (start.weekday(),)
        monday = start - timedelta(days=start.weekday())
        week = 0
        while True:
            stop = False
            for wd in days:
                d = monday + timedelta(days=7 * week + wd)
                if d < start:
                    continue
                if done(d):
                    stop = True
                    break
                out.append(d)
            if stop or len(out) > MAX_OCCURRENCES:
                break
            week += rule.interval

    if len(out) > MAX_OCCURRENCES:
        raise RuleError(f"series expands to more than {MAX_OCCURRENCES} occurrences")
    return out


@dataclass(frozen=True)
class Instant:
    utc: datetime
    note: str | None  # set when the wall time was shifted or ambiguous


def localize(day: date, wall: time, tz: ZoneInfo) -> Instant:
    """Turn a local date + wall time into a UTC instant, RFC 5545 style.

    * Nonexistent times (spring-forward gap) use the offset *before* the gap,
      so 02:30 becomes 03:30 on the day clocks jump from 02:00 to 03:00.
    * Ambiguous times (fall-back overlap) resolve to the earlier instant.
    zoneinfo's ``fold=0`` implements exactly both rules (PEP 495).
    """
    local = datetime.combine(day, wall, tzinfo=tz)  # fold=0
    utc = local.astimezone(timezone.utc)
    back = utc.astimezone(tz)
    if back.replace(tzinfo=None) != local.replace(tzinfo=None):
        note = f"{wall:%H:%M} does not exist on {day}; shifted to {back:%H:%M}"
    elif local.replace(fold=1).utcoffset() != local.utcoffset():
        note = f"{wall:%H:%M} occurs twice on {day}; using the first"
    else:
        note = None
    return Instant(utc=utc, note=note)


def to_local(ts: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
