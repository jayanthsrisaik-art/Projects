"""Event schema, parsing and validation for request-log events (JSONL)."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Event:
    ts: datetime  # timezone-aware UTC
    user_id: str
    endpoint: str
    latency_ms: float
    status: int

    @property
    def is_error(self) -> bool:
        return self.status >= 500


class InvalidEvent(ValueError):
    pass


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp that carries an explicit offset; return UTC."""
    if not isinstance(value, str):
        raise InvalidEvent("ts must be a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidEvent(f"unparseable ts {value!r}") from exc
    if dt.tzinfo is None:
        raise InvalidEvent(f"ts {value!r} has no timezone offset")
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def day_floor(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_event(raw: dict) -> Event:
    if not isinstance(raw, dict):
        raise InvalidEvent("event must be a JSON object")
    missing = [k for k in ("ts", "user_id", "endpoint", "latency_ms", "status") if k not in raw]
    if missing:
        raise InvalidEvent(f"missing field(s): {', '.join(missing)}")
    ts = parse_ts(raw["ts"])
    user = raw["user_id"]
    if not isinstance(user, str) or not user.strip():
        raise InvalidEvent("user_id must be a non-empty string")
    endpoint = raw["endpoint"]
    if not isinstance(endpoint, str) or not endpoint.startswith("/"):
        raise InvalidEvent("endpoint must be a string starting with '/'")
    lat = raw["latency_ms"]
    if isinstance(lat, bool) or not isinstance(lat, (int, float)) or not math.isfinite(lat) or lat < 0:
        raise InvalidEvent(f"latency_ms must be a finite number >= 0, got {lat!r}")
    status = raw["status"]
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise InvalidEvent(f"status must be an integer HTTP code, got {status!r}")
    return Event(ts, user, endpoint, float(lat), status)


def read_jsonl(path: str):
    """Yield (line_no, raw_line, Event | InvalidEvent) for each non-blank line."""
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                yield line_no, text, parse_event(json.loads(text))
            except json.JSONDecodeError as exc:
                yield line_no, text, InvalidEvent(f"invalid JSON: {exc.msg}")
            except InvalidEvent as exc:
                yield line_no, text, exc


def event_to_json(e: Event) -> str:
    return json.dumps({"ts": fmt_ts(e.ts), "user_id": e.user_id, "endpoint": e.endpoint,
                       "latency_ms": round(e.latency_ms, 3), "status": e.status}, separators=(",", ":"))
