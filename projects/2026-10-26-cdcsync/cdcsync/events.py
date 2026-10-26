"""Parsing and validation of Debezium-style change events.

One JSON object per line. Two record kinds:

* change:  {"type": "change", "table": "orders", "op": "u",
            "key": {"id": 5}, "after": {...}, "before": {...} | null,
            "lsn": 1042, "tx": "tx-17" | null, "ts_ms": 1700000000000}
  op is one of c (create), u (update), d (delete), r (snapshot read).
* tx_end:  {"type": "tx_end", "tx": "tx-17", "event_count": 3}

Everything that reaches SQL as an identifier is validated here, because table
and column names are interpolated into statements (values never are).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
META_PREFIX = "_cdc_"
OPS = {"c", "u", "d", "r"}
SCALARS = (str, int, float, bool, type(None))


class RejectError(ValueError):
    """The record is malformed; it is quarantined instead of applied."""


@dataclass(frozen=True)
class Change:
    table: str
    op: str
    key: dict[str, Any]
    after: dict[str, Any] | None
    lsn: int
    tx: str | None

    def to_json(self) -> str:
        return json.dumps(
            {"type": "change", "table": self.table, "op": self.op, "key": self.key,
             "after": self.after, "lsn": self.lsn, "tx": self.tx},
            sort_keys=True,
        )


@dataclass(frozen=True)
class TxEnd:
    tx: str
    event_count: int


def _check_ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not IDENT.match(name):
        raise RejectError(f"invalid {what} name: {name!r}")
    if name.lower().startswith(META_PREFIX) or name.lower().startswith("sqlite_"):
        raise RejectError(f"reserved {what} name: {name!r}")
    return name


def _check_row(row: Any, what: str) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        raise RejectError(f"{what} must be a non-empty object")
    for col, val in row.items():
        _check_ident(col, "column")
        if not isinstance(val, SCALARS):
            raise RejectError(f"{what}.{col} is not a scalar")
    return row


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_line(line: str) -> Change | TxEnd:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RejectError(f"invalid JSON: {exc.msg}") from None
    if not isinstance(rec, dict):
        raise RejectError("record is not an object")
    kind = rec.get("type")
    if kind == "tx_end":
        tx = rec.get("tx")
        count = rec.get("event_count")
        if not isinstance(tx, str) or not tx:
            raise RejectError("tx_end without tx id")
        if not _is_int(count) or count < 1:
            raise RejectError("tx_end.event_count must be a positive integer")
        return TxEnd(tx, count)
    if kind != "change":
        raise RejectError(f"unknown record type: {kind!r}")

    table = _check_ident(rec.get("table"), "table")
    op = rec.get("op")
    if op not in OPS:
        raise RejectError(f"unknown op: {op!r}")
    lsn = rec.get("lsn")
    if not _is_int(lsn) or lsn < 0:
        raise RejectError("lsn must be a non-negative integer")
    tx = rec.get("tx")
    if tx is not None and (not isinstance(tx, str) or not tx):
        raise RejectError("tx must be a non-empty string or null")
    if op == "r" and tx is not None:
        raise RejectError("snapshot reads cannot belong to a transaction")

    key = _check_row(rec.get("key"), "key")
    for col, val in key.items():
        if not (isinstance(val, str) or _is_int(val)):
            raise RejectError(f"key.{col} must be a string or integer")
    after = rec.get("after")
    if op == "d":
        if after is not None:
            raise RejectError("delete must have after = null")
    else:
        after = _check_row(after, "after")
        for col, val in key.items():
            if col not in after or after[col] != val:
                raise RejectError(f"key column {col!r} missing from or differs in after")
    return Change(table, op, dict(key), None if after is None else dict(after), lsn, tx)
