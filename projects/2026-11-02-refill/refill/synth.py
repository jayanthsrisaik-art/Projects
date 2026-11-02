"""Seeded synthetic source data and correction scenarios for the demo pipeline."""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import Callable

from .partitions import Calendar

REGIONS = ("central", "east", "north", "south", "west")
Data = dict[str, list[tuple]]


def generate(cal: Calendar, seed: int = 7, customers: int = 300, orders_per_day: int = 60) -> Data:
    rng = random.Random(seed)
    dim = [("*", c, rng.choice(REGIONS)) for c in range(1, customers + 1)]
    days = cal.partitions("day")
    orders, refunds = [], []
    oid = 0
    for i, d in enumerate(days):
        for _ in range(rng.randint(orders_per_day - 15, orders_per_day + 15)):
            oid += 1
            cust = rng.randint(1, customers)
            amt = rng.randint(500, 25_000)
            orders.append((d, oid, cust, amt))
            if rng.random() < 0.05:  # refunded 1-10 days later, if still in range
                j = i + rng.randint(1, 10)
                if j < len(days):
                    refunds.append((days[j], oid, cust, rng.randint(100, amt)))
    return {"raw_orders": orders, "raw_refunds": refunds, "dim_customer": dim}


@dataclass
class Scenario:
    name: str
    description: str
    changes: list[str]                 # CLI-style change specs (the declared roots)
    mutate: Callable[[Data], Data]


def _late_orders(data: Data) -> Data:
    out = copy.deepcopy(data)
    rng = random.Random(101)
    oid = max(r[1] for r in out["raw_orders"])
    for d in ("2026-10-05", "2026-10-06"):
        for _ in range(15):
            oid += 1
            out["raw_orders"].append((d, oid, rng.randint(1, 300), rng.randint(500, 25_000)))
    return out


def _refund_fix(data: Data) -> Data:
    out = copy.deepcopy(data)
    out["raw_refunds"] = [
        (d, o, c, a * 2 if d == "2026-09-14" else a) for d, o, c, a in out["raw_refunds"]
    ]
    return out


def _region_move(data: Data) -> Data:
    """Move one low-activity customer to another region (dimension correction)."""
    out = copy.deepcopy(data)
    counts: dict[int, int] = {}
    for _, _, c, _ in out["raw_orders"]:
        counts[c] = counts.get(c, 0) + 1
    cust = min(counts, key=lambda c: (counts[c], c))
    out["dim_customer"] = [
        (p, c, ("north" if r != "north" else "south") if c == cust else r)
        for p, c, r in out["dim_customer"]
    ]
    return out


def _relanded_identical(data: Data) -> Data:
    """A partition re-delivered byte-for-byte: rows are identical."""
    return copy.deepcopy(data)


SCENARIOS = [
    Scenario("late_orders", "30 late-arriving orders land in 2026-10-05 and 2026-10-06",
             ["raw_orders:2026-10-05..2026-10-06"], _late_orders),
    Scenario("refund_fix", "refund amounts on 2026-09-14 were loaded at half value",
             ["raw_refunds:2026-09-14"], _refund_fix),
    Scenario("region_move", "one low-activity customer is re-assigned to another region",
             ["dim_customer:*"], _region_move),
    Scenario("relanded_identical", "raw_orders 2026-08-20 is re-delivered with identical rows",
             ["raw_orders:2026-08-20"], _relanded_identical),
]
