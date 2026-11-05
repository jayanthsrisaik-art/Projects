"""Core data types: events, window specs and mergeable accumulators."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


class InvalidEvent(ValueError):
    """Raised when a source record cannot be turned into an Event."""


@dataclass(frozen=True)
class Event:
    id: str
    key: str
    ts: int  # event time, epoch seconds
    value: int
    arrival: int | None = None  # processing time, epoch seconds (optional)


def _parse_time(raw, field_name: str) -> int:
    if isinstance(raw, bool):
        raise InvalidEvent(f"{field_name} must be epoch seconds or ISO-8601, got bool")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise InvalidEvent(f"{field_name}: not ISO-8601: {raw!r}") from exc
        if dt.tzinfo is None:
            raise InvalidEvent(f"{field_name}: timestamp needs an explicit offset: {raw!r}")
        return int(dt.astimezone(timezone.utc).timestamp())
    raise InvalidEvent(f"{field_name} must be epoch seconds or ISO-8601, got {type(raw).__name__}")


def parse_event(line: str) -> Event:
    """Parse one JSONL record. Raises InvalidEvent with a readable reason."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise InvalidEvent(f"bad json: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise InvalidEvent("record is not a JSON object")
    for name in ("id", "key", "ts", "value"):
        if name not in obj:
            raise InvalidEvent(f"missing field {name!r}")
    if not isinstance(obj["id"], str) or not obj["id"]:
        raise InvalidEvent("id must be a non-empty string")
    if not isinstance(obj["key"], str) or not obj["key"]:
        raise InvalidEvent("key must be a non-empty string")
    value = obj["value"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEvent("value must be an integer")
    ts = _parse_time(obj["ts"], "ts")
    arrival = obj.get("arrival")
    arrival = None if arrival is None else _parse_time(arrival, "arrival")
    return Event(obj["id"], obj["key"], ts, value, arrival)


def event_to_json(e: Event) -> str:
    obj = {"id": e.id, "key": e.key, "ts": e.ts, "value": e.value}
    if e.arrival is not None:
        obj["arrival"] = e.arrival
    return json.dumps(obj, separators=(",", ":"))


@dataclass(frozen=True)
class WindowSpec:
    """tumbling(size) | hopping(size, slide) | session(gap). All in seconds."""

    kind: str
    size: int = 0
    slide: int = 0
    gap: int = 0

    def __post_init__(self):
        if self.kind == "tumbling":
            if self.size <= 0:
                raise ValueError("tumbling size must be > 0")
        elif self.kind == "hopping":
            if self.size <= 0 or self.slide <= 0:
                raise ValueError("hopping size and slide must be > 0")
            if self.size % self.slide:
                raise ValueError("hopping size must be a multiple of slide")
        elif self.kind == "session":
            if self.gap <= 0:
                raise ValueError("session gap must be > 0")
        else:
            raise ValueError(f"unknown window kind {self.kind!r}")

    @property
    def merging(self) -> bool:
        return self.kind == "session"

    def assign(self, ts: int) -> list[tuple[int, int]]:
        """Half-open [start, end) windows containing ts.

        For sessions this is the proto-window [ts, ts + gap) that is merged
        with overlapping sessions of the same key.
        """
        if self.kind == "tumbling":
            start = ts - (ts % self.size)
            return [(start, start + self.size)]
        if self.kind == "hopping":
            last = ts - (ts % self.slide)
            first = last - self.size + self.slide
            return [(s, s + self.size) for s in range(first, last + 1, self.slide)]
        return [(ts, ts + self.gap)]

    def describe(self) -> str:
        if self.kind == "tumbling":
            return f"tumbling:{self.size}"
        if self.kind == "hopping":
            return f"hopping:{self.size}:{self.slide}"
        return f"session:{self.gap}"


def parse_window(text: str) -> WindowSpec:
    """'tumbling:60', 'hopping:300:60', 'session:120'."""
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts[1:]]
    except ValueError as exc:
        raise ValueError(f"bad window spec {text!r}") from exc
    kind = parts[0]
    if kind == "tumbling" and len(nums) == 1:
        return WindowSpec("tumbling", size=nums[0])
    if kind == "hopping" and len(nums) == 2:
        return WindowSpec("hopping", size=nums[0], slide=nums[1])
    if kind == "session" and len(nums) == 1:
        return WindowSpec("session", gap=nums[0])
    raise ValueError(f"bad window spec {text!r} (use tumbling:S, hopping:S:SLIDE or session:GAP)")


@dataclass
class Acc:
    """Mergeable aggregate: count/sum/min/max (+ mean) and the ids it holds.

    The id set makes the operator idempotent against at-least-once sources:
    a re-delivered event is recognised and ignored.
    """

    count: int = 0
    sum: int = 0
    min: int | None = None
    max: int | None = None
    ids: set = field(default_factory=set)

    def add(self, e: Event) -> bool:
        if e.id in self.ids:
            return False
        self.ids.add(e.id)
        self.count += 1
        self.sum += e.value
        self.min = e.value if self.min is None else min(self.min, e.value)
        self.max = e.value if self.max is None else max(self.max, e.value)
        return True

    def merge(self, other: "Acc") -> None:
        # Sessions of one key never share an id (duplicates are rejected before
        # insertion), so the counts can simply be added.
        self.ids |= other.ids
        self.count += other.count
        self.sum += other.sum
        if other.min is not None:
            self.min = other.min if self.min is None else min(self.min, other.min)
            self.max = other.max if self.max is None else max(self.max, other.max)

    def values(self) -> dict:
        return {
            "count": self.count,
            "sum": self.sum,
            "min": self.min,
            "max": self.max,
            "mean": round(self.sum / self.count, 6) if self.count else None,
        }

    def to_state(self) -> dict:
        return {"count": self.count, "sum": self.sum, "min": self.min, "max": self.max,
                "ids": sorted(self.ids)}

    @classmethod
    def from_state(cls, d: dict) -> "Acc":
        return cls(d["count"], d["sum"], d["min"], d["max"], set(d["ids"]))
