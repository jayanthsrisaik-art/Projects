"""SQLite persistence: groups, members, expenses, payments, idempotency keys.

Each public method opens its own connection and runs in one transaction.
Writes use ``BEGIN IMMEDIATE`` so the read-check-write sequences
(idempotency lookup, version check, rounding-bias read) are serialised across
threads *and* processes sharing the database file.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from fractions import Fraction
from typing import Any, Callable, Iterator

from .money import MoneyError, exponent_for, format_amount, parse_amount
from .splits import SplitError, compute_split

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    currency TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    name TEXT NOT NULL,
    UNIQUE (group_id, name)
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    description TEXT NOT NULL,
    payer_id INTEGER NOT NULL REFERENCES members(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    split_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS expense_shares (
    expense_id INTEGER NOT NULL REFERENCES expenses(id),
    member_id INTEGER NOT NULL REFERENCES members(id),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    bias TEXT NOT NULL,                 -- allocated - exact, as "num/den"
    PRIMARY KEY (expense_id, member_id)
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    from_member INTEGER NOT NULL REFERENCES members(id),
    to_member INTEGER NOT NULL REFERENCES members(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    group_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status INTEGER NOT NULL,
    response TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (group_id, key)
);
CREATE INDEX IF NOT EXISTS ix_expenses_group ON expenses(group_id, deleted);
CREATE INDEX IF NOT EXISTS ix_payments_group ON payments(group_id);
"""

# Net balance per member: paid - owed + payments sent - payments received.
BALANCE_SQL = """
SELECT m.id AS member_id, m.name,
       COALESCE((SELECT SUM(e.amount) FROM expenses e
                  WHERE e.payer_id = m.id AND e.deleted = 0), 0)
     - COALESCE((SELECT SUM(s.amount) FROM expense_shares s
                   JOIN expenses e ON e.id = s.expense_id
                  WHERE s.member_id = m.id AND e.deleted = 0), 0)
     + COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.from_member = m.id), 0)
     - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.to_member = m.id), 0)
       AS balance
  FROM members m
 WHERE m.group_id = ?
 ORDER BY m.id
"""


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    pass


class VersionConflict(Exception):
    def __init__(self, current: int | None):
        super().__init__("version mismatch")
        self.current = current


class IdempotencyMismatch(Exception):
    pass


def request_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Ledger:
    def __init__(self, path: str, *, drift_aware: bool = True):
        self.path = path
        self.drift_aware = drift_aware
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _group(c: sqlite3.Connection, gid: int) -> sqlite3.Row:
        row = c.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
        if row is None:
            raise NotFound(f"group {gid} not found")
        return row

    @staticmethod
    def _member_ids(c: sqlite3.Connection, gid: int) -> list[int]:
        return [r[0] for r in c.execute("SELECT id FROM members WHERE group_id = ? ORDER BY id", (gid,))]

    def _prior_bias(self, c: sqlite3.Connection, gid: int, exclude_expense: int | None) -> dict[int, Fraction] | None:
        return self._all_bias(c, gid, exclude_expense) if self.drift_aware else None

    def _expense_json(self, c: sqlite3.Connection, row: sqlite3.Row, exp: int) -> dict:
        shares = c.execute(
            "SELECT member_id, amount FROM expense_shares WHERE expense_id = ? ORDER BY member_id",
            (row["id"],)).fetchall()
        return {
            "id": row["id"], "group_id": row["group_id"], "description": row["description"],
            "payer_id": row["payer_id"], "amount": format_amount(row["amount"], exp),
            "amount_minor": row["amount"], "split": json.loads(row["split_json"]),
            "shares": [{"member_id": s[0], "amount": format_amount(s[1], exp), "amount_minor": s[1]}
                       for s in shares],
            "version": row["version"], "deleted": bool(row["deleted"]),
        }

    def _validated_expense(self, c: sqlite3.Connection, gid: int, body: dict, exclude: int | None):
        g = self._group(c, gid)
        exp = exponent_for(g["currency"])
        members = self._member_ids(c, gid)
        desc = str(body.get("description", "")).strip()
        if not desc or len(desc) > 200:
            raise Invalid("description must be 1-200 characters")
        payer = body.get("payer_id")
        if payer not in members:
            raise Invalid(f"payer {payer} is not in this group")
        try:
            total = parse_amount(body.get("amount"), exp)
            split = compute_split(total, body.get("split") or {}, exp, members,
                                  self._prior_bias(c, gid, exclude))
        except (MoneyError, SplitError) as e:
            raise Invalid(str(e)) from None
        return exp, desc, payer, total, split

    def _with_idempotency(self, c: sqlite3.Connection, gid: int, key: str | None, body: Any,
                          action: Callable[[], dict]) -> tuple[dict, bool]:
        """Run ``action`` once per (group, key). Returns (response, replayed)."""
        if key is None:
            return action(), False
        h = request_hash(body)
        row = c.execute("SELECT request_hash, response FROM idempotency_keys WHERE group_id = ? AND key = ?",
                        (gid, key)).fetchone()
        if row is not None:
            if row[0] != h:
                raise IdempotencyMismatch("Idempotency-Key was already used with a different request body")
            return json.loads(row[1]), True
        resp = action()
        c.execute("INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?, ?)",
                  (gid, key, h, 201, json.dumps(resp), time.time()))
        return resp, False

    # ----------------------------------------------------------------- groups
    def create_group(self, name: str, currency: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 100:
            raise Invalid("group name must be 1-100 characters")
        try:
            exponent_for(currency)
        except MoneyError as e:
            raise Invalid(str(e)) from None
        with self._write() as c:
            cur = c.execute("INSERT INTO groups (name, currency, created_at) VALUES (?, ?, ?)",
                            (name, currency, time.time()))
            return {"id": cur.lastrowid, "name": name, "currency": currency,
                    "exponent": exponent_for(currency), "members": []}

    def get_group(self, gid: int) -> dict:
        with self._conn() as c:
            g = self._group(c, gid)
            members = [{"id": r[0], "name": r[1]} for r in
                       c.execute("SELECT id, name FROM members WHERE group_id = ? ORDER BY id", (gid,))]
            return {"id": g["id"], "name": g["name"], "currency": g["currency"],
                    "exponent": exponent_for(g["currency"]), "members": members}

    def add_member(self, gid: int, name: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 60:
            raise Invalid("member name must be 1-60 characters")
        with self._write() as c:
            self._group(c, gid)
            try:
                cur = c.execute("INSERT INTO members (group_id, name) VALUES (?, ?)", (gid, name))
            except sqlite3.IntegrityError:
                raise Invalid(f"member {name!r} already exists in this group") from None
            return {"id": cur.lastrowid, "name": name}

    # --------------------------------------------------------------- expenses
    def preview_split(self, gid: int, body: dict) -> dict:
        with self._conn() as c:
            exp, _, _, total, split = self._validated_expense(c, gid, body, body.get("expense_id"))
            return {"amount_minor": total,
                    "shares": [{"member_id": m, "amount": format_amount(v, exp), "amount_minor": v}
                               for m, v in sorted(split.shares.items())]}

    def create_expense(self, gid: int, body: dict, idem_key: str | None) -> tuple[dict, bool]:
        with self._write() as c:
            self._group(c, gid)

            def action() -> dict:
                exp, desc, payer, total, split = self._validated_expense(c, gid, body, None)
                now = time.time()
                cur = c.execute(
                    """INSERT INTO expenses (group_id, description, payer_id, amount, split_json,
                                             created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (gid, desc, payer, total, json.dumps(body["split"], sort_keys=True), now, now))
                eid = cur.lastrowid
                c.executemany("INSERT INTO expense_shares VALUES (?, ?, ?, ?)",
                              [(eid, m, v, str(split.bias[m])) for m, v in split.shares.items()])
                row = c.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone()
                return self._expense_json(c, row, exp)

            return self._with_idempotency(c, gid, idem_key, {"op": "expense", **body}, action)

    def _expense_row(self, c: sqlite3.Connection, eid: int) -> sqlite3.Row:
        row = c.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone()
        if row is None or row["deleted"]:
            raise NotFound(f"expense {eid} not found")
        return row

    def get_expense(self, eid: int) -> dict:
        with self._conn() as c:
            row = self._expense_row(c, eid)
            return self._expense_json(c, row, exponent_for(self._group(c, row["group_id"])["currency"]))

    def list_expenses(self, gid: int) -> list[dict]:
        with self._conn() as c:
            exp = exponent_for(self._group(c, gid)["currency"])
            rows = c.execute("SELECT * FROM expenses WHERE group_id = ? AND deleted = 0 ORDER BY id DESC",
                             (gid,)).fetchall()
            return [self._expense_json(c, r, exp) for r in rows]

    def update_expense(self, eid: int, body: dict, expected_version: int) -> dict:
        with self._write() as c:
            row = self._expense_row(c, eid)
            if row["version"] != expected_version:
                raise VersionConflict(row["version"])
            gid = row["group_id"]
            exp, desc, payer, total, split = self._validated_expense(c, gid, body, eid)
            cur = c.execute(
                """UPDATE expenses SET description = ?, payer_id = ?, amount = ?, split_json = ?,
                          version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND deleted = 0""",
                (desc, payer, total, json.dumps(body["split"], sort_keys=True), time.time(),
                 eid, expected_version))
            if cur.rowcount != 1:  # unreachable under BEGIN IMMEDIATE; kept as a guard
                raise VersionConflict(None)
            c.execute("DELETE FROM expense_shares WHERE expense_id = ?", (eid,))
            c.executemany("INSERT INTO expense_shares VALUES (?, ?, ?, ?)",
                          [(eid, m, v, str(split.bias[m])) for m, v in split.shares.items()])
            return self._expense_json(c, c.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone(), exp)

    def delete_expense(self, eid: int, expected_version: int) -> None:
        with self._write() as c:
            row = self._expense_row(c, eid)
            if row["version"] != expected_version:
                raise VersionConflict(row["version"])
            c.execute("UPDATE expenses SET deleted = 1, version = version + 1, updated_at = ? WHERE id = ?",
                      (time.time(), eid))

    # --------------------------------------------------------------- payments
    def record_payment(self, gid: int, body: dict, idem_key: str | None) -> tuple[dict, bool]:
        with self._write() as c:
            g = self._group(c, gid)
            exp = exponent_for(g["currency"])

            def action() -> dict:
                members = self._member_ids(c, gid)
                frm, to = body.get("from_member"), body.get("to_member")
                if frm not in members or to not in members:
                    raise Invalid("both members must belong to this group")
                if frm == to:
                    raise Invalid("a member cannot pay themselves")
                try:
                    amount = parse_amount(body.get("amount"), exp)
                except MoneyError as e:
                    raise Invalid(str(e)) from None
                cur = c.execute(
                    "INSERT INTO payments (group_id, from_member, to_member, amount, created_at) VALUES (?, ?, ?, ?, ?)",
                    (gid, frm, to, amount, time.time()))
                return {"id": cur.lastrowid, "from_member": frm, "to_member": to,
                        "amount": format_amount(amount, exp), "amount_minor": amount}

            return self._with_idempotency(c, gid, idem_key, {"op": "payment", **body}, action)

    def list_payments(self, gid: int) -> list[dict]:
        with self._conn() as c:
            exp = exponent_for(self._group(c, gid)["currency"])
            return [{"id": r["id"], "from_member": r["from_member"], "to_member": r["to_member"],
                     "amount": format_amount(r["amount"], exp), "amount_minor": r["amount"]}
                    for r in c.execute("SELECT * FROM payments WHERE group_id = ? ORDER BY id DESC", (gid,))]

    # --------------------------------------------------------------- balances
    def balances(self, gid: int) -> dict[int, int]:
        with self._conn() as c:
            self._group(c, gid)
            return {r["member_id"]: r["balance"] for r in c.execute(BALANCE_SQL, (gid,))}

    def rounding_drift(self, gid: int) -> dict[int, Fraction]:
        """Cumulative allocated-minus-exact per member over live expenses."""
        with self._conn() as c:
            self._group(c, gid)
            drift = {m: Fraction(0) for m in self._member_ids(c, gid)}
            drift.update(self._all_bias(c, gid, None))
            return drift

    @staticmethod
    def _all_bias(c: sqlite3.Connection, gid: int, exclude_expense: int | None) -> dict[int, Fraction]:
        out: dict[int, Fraction] = {}
        for mid, b in c.execute(
                """SELECT s.member_id, s.bias FROM expense_shares s JOIN expenses e ON e.id = s.expense_id
                    WHERE e.group_id = ? AND e.deleted = 0 AND e.id IS NOT ?""", (gid, exclude_expense)):
            out[mid] = out.get(mid, Fraction(0)) + Fraction(b)
        return out
