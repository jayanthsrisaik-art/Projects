"""Schema contracts: declarative field rules and per-record validation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

TYPES = {"string", "int", "float", "bool", "timestamp"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = True
    enum: tuple = ()
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class Contract:
    name: str
    key: str
    time_field: str
    fields: tuple[Field, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, d: dict) -> "Contract":
        fields = []
        for f in d["fields"]:
            if f["type"] not in TYPES:
                raise ValueError(f"unknown type {f['type']!r} for field {f['name']!r}")
            fields.append(Field(f["name"], f["type"], f.get("required", True),
                                tuple(f.get("enum", ())), f.get("min"), f.get("max")))
        c = cls(d["name"], d["key"], d["time_field"], tuple(fields))
        by_name = {f.name: f for f in fields}
        if c.key not in by_name or not by_name[c.key].required:
            raise ValueError("key must be a required field")
        if c.time_field not in by_name or by_name[c.time_field].type != "timestamp" \
                or not by_name[c.time_field].required:
            raise ValueError("time_field must be a required timestamp field")
        return c

    @classmethod
    def load(cls, path: str) -> "Contract":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @property
    def names(self) -> set[str]:
        return {f.name for f in self.fields}


def parse_ts(value: Any) -> datetime:
    """Parse an ISO-8601 string into an aware UTC datetime (naive input is rejected)."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return dt.astimezone(timezone.utc)


def _check_type(f: Field, v: Any) -> str | None:
    if f.type == "string":
        ok = isinstance(v, str)
    elif f.type == "int":
        ok = isinstance(v, int) and not isinstance(v, bool)
    elif f.type == "float":
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
    elif f.type == "bool":
        ok = isinstance(v, bool)
    else:
        try:
            parse_ts(v)
            ok = True
        except ValueError:
            ok = False
    return None if ok else f"{f.name}: expected {f.type}"


def validate(contract: Contract, record: Any) -> list[str]:
    """Return a list of violation strings (empty means valid)."""
    if not isinstance(record, dict):
        return ["record: not a JSON object"]
    errors: list[str] = []
    for f in contract.fields:
        if f.name not in record or record[f.name] is None:
            if f.required:
                errors.append(f"{f.name}: missing required")
            continue
        v = record[f.name]
        err = _check_type(f, v)
        if err:
            errors.append(err)
            continue
        if f.enum and v not in f.enum:
            errors.append(f"{f.name}: not in enum")
        if f.type in ("int", "float"):
            if f.min is not None and v < f.min:
                errors.append(f"{f.name}: below min {f.min}")
            if f.max is not None and v > f.max:
                errors.append(f"{f.name}: above max {f.max}")
    return errors
