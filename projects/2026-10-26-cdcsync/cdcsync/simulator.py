"""Synthetic source database that emits a Debezium-style change stream.

The simulator keeps the authoritative source state in memory, emits:

1. an initial snapshot (op "r") of the rows that existed when capture began,
2. a stream of transactions (op c/u/d plus a tx_end marker), including an
   additive schema change (customers.loyalty_tier) half-way through and
   re-inserts of previously deleted keys,

and can write its final state to a SQLite file for reconciliation. `chaos()`
then degrades delivery the way real pipelines do: bounded reordering,
duplicates and a few malformed lines.
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

CITIES = ["Lisbon", "Austin", "Pune", "Osaka", "Nairobi", "Leeds", "Recife", "Graz"]
STATUSES = ["placed", "paid", "shipped", "delivered", "refunded"]
TIERS = ["bronze", "silver", "gold"]


class SourceSim:
    def __init__(self, seed: int = 7, customers: int = 40, orders: int = 80):
        self.rng = random.Random(seed)
        self.lsn = 1000
        self.tx_seq = 0
        self.has_tier = False
        self.state: dict[str, dict[int, dict]] = {"customers": {}, "orders": {}}
        self.next_id = {"customers": 1, "orders": 1}
        self.deleted: dict[str, list[int]] = {"customers": [], "orders": []}
        for _ in range(customers):  # history before capture starts: not emitted
            self._insert_customer()
        for _ in range(orders):
            self._insert_order()

    # -------------------------------------------------------------- mutations
    def _new_id(self, table: str) -> int:
        if self.deleted[table] and self.rng.random() < 0.3:
            return self.deleted[table].pop(self.rng.randrange(len(self.deleted[table])))
        i = self.next_id[table]
        self.next_id[table] += 1
        return i

    def _insert_customer(self) -> tuple[str, int]:
        cid = self._new_id("customers")
        row = {
            "id": cid,
            "name": f"customer-{cid}",
            "email": f"c{cid}.{self.rng.randrange(10**4)}@example.test",
            "city": self.rng.choice(CITIES),
        }
        if self.has_tier:
            row["loyalty_tier"] = self.rng.choice(TIERS)
        self.state["customers"][cid] = row
        return "customers", cid

    def _insert_order(self) -> tuple[str, int]:
        oid = self._new_id("orders")
        customer = self.rng.choice(sorted(self.state["customers"])) if self.state["customers"] else None
        self.state["orders"][oid] = {
            "id": oid,
            "customer_id": customer,
            "amount_cents": self.rng.randrange(500, 50_000),
            "status": "placed",
        }
        return "orders", oid

    def _random_op(self) -> tuple[str, str, int]:
        r = self.rng.random()
        customers, orders = self.state["customers"], self.state["orders"]
        if r < 0.15 or not customers:
            return ("c", *self._insert_customer())
        if r < 0.40 or not orders:
            return ("c", *self._insert_order())
        if r < 0.60:
            cid = self.rng.choice(sorted(customers))
            row = customers[cid]
            row["email"] = f"c{cid}.{self.rng.randrange(10**4)}@example.test"
            row["city"] = self.rng.choice(CITIES)
            if self.has_tier:
                row["loyalty_tier"] = self.rng.choice(TIERS)
            return "u", "customers", cid
        if r < 0.85:
            oid = self.rng.choice(sorted(orders))
            orders[oid]["status"] = self.rng.choice(STATUSES)
            orders[oid]["amount_cents"] += self.rng.randrange(-100, 100)
            return "u", "orders", oid
        table = "orders" if r < 0.95 else "customers"
        rid = self.rng.choice(sorted(self.state[table]))
        del self.state[table][rid]
        self.deleted[table].append(rid)
        return "d", table, rid

    def _event(self, op: str, table: str, rid: int, tx: str | None) -> dict:
        self.lsn += 1
        after = None if op == "d" else dict(self.state[table][rid])
        if after is not None and table == "customers" and self.has_tier:
            after.setdefault("loyalty_tier", None)
        return {"type": "change", "table": table, "op": op, "key": {"id": rid},
                "after": after, "lsn": self.lsn, "tx": tx, "ts_ms": 1_760_000_000_000 + self.lsn}

    # --------------------------------------------------------------- emitters
    def snapshot(self) -> list[dict]:
        events = []
        for table in ("customers", "orders"):
            for rid in sorted(self.state[table]):
                events.append(self._event("r", table, rid, None))
        return events

    def stream(self, transactions: int) -> list[dict]:
        events: list[dict] = []
        for i in range(transactions):
            if i == transactions // 2:
                self.has_tier = True  # ALTER TABLE customers ADD COLUMN loyalty_tier
                for row in self.state["customers"].values():
                    row["loyalty_tier"] = None
            self.tx_seq += 1
            tx = f"tx-{self.tx_seq}"
            # Emit right after each mutation so the row image is the state at that LSN.
            tx_events = [self._event(*self._random_op(), tx) for _ in range(self.rng.randint(1, 4))]
            events.extend(tx_events)
            events.append({"type": "tx_end", "tx": tx, "event_count": len(tx_events)})
        return events

    def write_source_db(self, path: str | Path) -> None:
        path = Path(path)
        if path.exists():
            path.unlink()
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name, email, city, loyalty_tier)")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id, amount_cents, status)")
        for row in self.state["customers"].values():
            conn.execute(
                "INSERT INTO customers VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["name"], row["email"], row["city"], row.get("loyalty_tier")),
            )
        for row in self.state["orders"].values():
            conn.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?)",
                (row["id"], row["customer_id"], row["amount_cents"], row["status"]),
            )
        conn.commit()
        conn.close()


def chaos(
    events: list[dict],
    seed: int = 11,
    window: int = 20,
    dup_rate: float = 0.05,
    malformed: int = 3,
) -> list[str]:
    """Return JSONL lines with bounded reordering, duplicates and junk lines.

    Each event moves at most `window` positions from where it was produced.
    """
    rng = random.Random(seed)
    keyed = [(i + rng.uniform(0, window), json.dumps(e, sort_keys=True)) for i, e in enumerate(events)]
    for i, e in enumerate(events):
        if rng.random() < dup_rate:
            keyed.append((i + rng.uniform(0, window), json.dumps(e, sort_keys=True)))
    junk = [
        '{"type": "change", "table": "orders"',  # truncated JSON
        json.dumps({"type": "change", "table": "orders; DROP TABLE orders", "op": "c",
                    "key": {"id": 1}, "after": {"id": 1}, "lsn": 1, "tx": None}),
        json.dumps({"type": "change", "table": "orders", "op": "u", "key": {"id": 2},
                    "after": {"id": 2}, "lsn": "late", "tx": None}),
        json.dumps({"type": "heartbeat"}),
    ]
    for j in range(malformed):
        keyed.append((rng.uniform(0, len(events)), junk[j % len(junk)]))
    keyed.sort(key=lambda kv: kv[0])
    return [line for _, line in keyed]


def generate(out_dir: str | Path, seed: int = 7, transactions: int = 300, window: int = 20,
             dup_rate: float = 0.05, malformed: int = 3) -> dict:
    """Write events.jsonl (chaotic), clean_events.jsonl (in order) and source.db."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sim = SourceSim(seed=seed)
    events = sim.snapshot() + sim.stream(transactions)
    (out / "clean_events.jsonl").write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8")
    lines = chaos(events, seed=seed + 1, window=window, dup_rate=dup_rate, malformed=malformed)
    (out / "events.jsonl").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    sim.write_source_db(out / "source.db")
    return {
        "events": len(events),
        "lines": len(lines),
        "transactions": transactions,
        "source_rows": {t: len(rows) for t, rows in sim.state.items()},
    }
