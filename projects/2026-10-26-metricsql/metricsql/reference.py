"""Independent pure-Python oracle for the sample shop model (samples/model.json).

It reads raw rows and computes metrics with plain loops and dicts, without SQL joins, so tests
and the benchmark can check compiled SQL against something that shares none of its code.
Supported group-bys: customers__region, orders__channel, metric_time__month; metrics listed in
SUPPORTED.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

SUPPORTED = ("revenue", "completed_revenue", "units", "orders", "buyers", "shipping_revenue", "refunds",
             "refund_count", "aov", "net_revenue")
GROUPS = ("customers__region", "orders__channel", "metric_time__month")


def compute(conn: sqlite3.Connection, metrics: list[str], group_by: list[str],
            channel_in: list[str] | None = None) -> dict[tuple, dict[str, float]]:
    for g in group_by:
        assert g in GROUPS, g
    region = dict(conn.execute("SELECT customer_id, region FROM customers"))
    orders = {oid: {"cust": cid, "date": d, "status": st, "channel": ch, "ship": fee}
              for oid, cid, d, st, ch, fee in conn.execute(
                  "SELECT order_id, customer_id, order_date, status, channel, shipping_fee FROM orders")}

    def key(order: dict, when: str) -> tuple:
        values = {"customers__region": region[order["cust"]], "orders__channel": order["channel"],
                  "metric_time__month": when[:7] + "-01"}
        return tuple(values[g] for g in group_by)

    def keep(order: dict) -> bool:
        return channel_in is None or order["channel"] in channel_in

    acc: dict[tuple, dict] = defaultdict(lambda: defaultdict(float))
    buyer_sets: dict[tuple, set] = defaultdict(set)
    for oid, o in orders.items():
        if keep(o):
            k = key(o, o["date"])
            acc[k]["orders"] += 1
            acc[k]["shipping_revenue"] += o["ship"]
            buyer_sets[k].add(o["cust"])
    for oid, qty, price in conn.execute("SELECT order_id, quantity, unit_price FROM order_items"):
        o = orders[oid]
        if keep(o):
            k = key(o, o["date"])
            acc[k]["revenue"] += qty * price
            acc[k]["units"] += qty
            if o["status"] == "completed":
                acc[k]["completed_revenue"] += qty * price
    for oid, rdate, amount in conn.execute("SELECT order_id, refund_date, amount FROM refunds"):
        o = orders[oid]
        if keep(o):
            k = key(o, rdate)  # refunds are dated by refund_date
            acc[k]["refunds"] += amount
            acc[k]["refund_count"] += 1

    out = {}
    for k, a in acc.items():
        a["buyers"] = len(buyer_sets.get(k, ()))
        a["aov"] = a["revenue"] / a["orders"] if a["orders"] else None
        a["net_revenue"] = a["revenue"] + a["shipping_revenue"] - a["refunds"]
        out[k] = {m: a[m] for m in metrics}
    return out
