"""Feature repository: feature-view definitions and typed source loading.

A feature view is a CSV of feature rows for one entity type.  Every row has
an event timestamp (when the fact was true in the world) and, optionally, a
created timestamp (when the row landed in the warehouse).  The two differ for
daily batch jobs, late data and backfilled corrections, and that gap is
exactly where training-set leakage comes from.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

MICROS = 1_000_000
_DTYPES = ("int", "float", "str", "bool")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class RepoError(ValueError):
    """Invalid repository definition or source data."""


def parse_ts(text: str) -> int:
    """Parse an ISO-8601 timestamp to integer UTC microseconds.

    Offsets are honoured; naive timestamps are treated as UTC.  Integer
    microseconds keep comparisons exact (no float rounding at boundaries).
    """
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * MICROS + delta.microseconds


def format_ts(us: int) -> str:
    dt = _EPOCH + timedelta(microseconds=us)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (f".{us % MICROS:06d}" if us % MICROS else "") + "Z"


def parse_value(text: str, dtype: str):
    if text == "":
        return None
    if dtype == "int":
        return int(text)
    if dtype == "float":
        return float(text)
    if dtype == "bool":
        low = text.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise ValueError(f"not a bool: {text!r}")
    return text


def format_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True)
class FeatureRow:
    idx: int          # position in the source file; final tie-breaker
    key: str
    ts: int           # event timestamp, UTC micros
    created: int      # created timestamp (== ts when the view has none)
    values: tuple

    @property
    def available_at(self) -> int:
        """Earliest instant at which a point-in-time reader may see this row."""
        return max(self.ts, self.created)

    @property
    def rank(self) -> tuple:
        """Total order used to choose among eligible rows (higher wins)."""
        return (self.ts, self.created, self.idx)


@dataclass
class FeatureView:
    name: str
    entity: str                     # join-key column name, shared with entity rows
    source: Path
    timestamp_field: str
    features: list[tuple[str, str]]  # (name, dtype)
    created_timestamp_field: str | None = None
    ttl_seconds: int | None = None
    rows: list[FeatureRow] = field(default_factory=list, repr=False)

    @property
    def ttl_us(self) -> int | None:
        return None if self.ttl_seconds is None else self.ttl_seconds * MICROS

    @property
    def feature_names(self) -> list[str]:
        return [f for f, _ in self.features]

    def output_columns(self) -> list[str]:
        return [f"{self.name}__{f}" for f in self.feature_names]

    def load(self) -> "FeatureView":
        self.rows = load_feature_rows(self)
        return self


@dataclass
class EntityFrame:
    """The 'spine' of a training set: one row per label, with its timestamp."""

    columns: list[str]
    rows: list[dict]           # raw string values, as read
    ts: list[int]              # parsed timestamp per row
    timestamp_field: str

    def keys(self, column: str) -> list[str | None]:
        if column not in self.columns:
            raise RepoError(f"entity rows have no column {column!r}")
        return [r[column] if r[column] != "" else None for r in self.rows]


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        raise RepoError(f"source not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def load_feature_rows(view: FeatureView) -> list[FeatureRow]:
    header, raw = _read_csv(view.source)
    needed = [view.entity, view.timestamp_field, *view.feature_names]
    if view.created_timestamp_field:
        needed.append(view.created_timestamp_field)
    missing = [c for c in needed if c not in header]
    if missing:
        raise RepoError(f"{view.name}: source {view.source.name} lacks columns {missing}")
    rows = []
    for i, r in enumerate(raw):
        line = i + 2  # header is line 1
        key = r[view.entity]
        if key == "":
            raise RepoError(f"{view.name}: line {line}: empty join key")
        try:
            ts = parse_ts(r[view.timestamp_field])
            created = parse_ts(r[view.created_timestamp_field]) if view.created_timestamp_field else ts
            values = tuple(parse_value(r[f], d) for f, d in view.features)
        except ValueError as exc:
            raise RepoError(f"{view.name}: line {line}: {exc}") from None
        rows.append(FeatureRow(i, key, ts, created, values))
    return rows


def load_entities(path: str | Path, timestamp_field: str = "event_timestamp") -> EntityFrame:
    path = Path(path)
    header, raw = _read_csv(path)
    if timestamp_field not in header:
        raise RepoError(f"entity file {path.name} lacks timestamp column {timestamp_field!r}")
    ts = []
    for i, r in enumerate(raw):
        try:
            ts.append(parse_ts(r[timestamp_field]))
        except ValueError as exc:
            raise RepoError(f"entities: line {i + 2}: {exc}") from None
    return EntityFrame(header, raw, ts, timestamp_field)


def load_repo(path: str | Path, load_rows: bool = True) -> dict[str, FeatureView]:
    """Load ``feature_repo.json``; source paths are relative to that file."""
    path = Path(path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    views: dict[str, FeatureView] = {}
    for v in spec.get("feature_views", []):
        try:
            name = v["name"]
            features = [(f["name"], f.get("dtype", "str")) for f in v["features"]]
            view = FeatureView(
                name=name,
                entity=v["entity"],
                source=(path.parent / v["source"]).resolve(),
                timestamp_field=v.get("timestamp_field", "event_timestamp"),
                created_timestamp_field=v.get("created_timestamp_field"),
                ttl_seconds=v.get("ttl_seconds"),
                features=features,
            )
        except KeyError as exc:
            raise RepoError(f"feature view missing field {exc}") from None
        if name in views:
            raise RepoError(f"duplicate feature view {name!r}")
        if "__" in name:
            raise RepoError(f"view name {name!r} may not contain '__'")
        bad = [d for _, d in features if d not in _DTYPES]
        if bad:
            raise RepoError(f"{name}: unknown dtypes {bad}")
        if len(set(view.feature_names)) != len(features):
            raise RepoError(f"{name}: duplicate feature names")
        if view.ttl_seconds is not None and view.ttl_seconds <= 0:
            raise RepoError(f"{name}: ttl_seconds must be positive")
        views[name] = view.load() if load_rows else view
    if not views:
        raise RepoError("repository defines no feature views")
    return views
