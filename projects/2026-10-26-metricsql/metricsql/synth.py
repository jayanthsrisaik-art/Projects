"""Deterministic synthetic e-commerce star schema in SQLite.

customers 1─* orders 1─* order_items *─1 products
                 1─* refunds        (an order can have 0, 1 or 2 refunds)
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, region TEXT, segment TEXT NOT NULL,
                        signup_date TEXT NOT NULL);
CREATE TABLE products (product_id INTEGER PRIMARY KEY, category TEXT NOT NULL, unit_cost REAL NOT NULL);
CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers,
                     order_date TEXT NOT NULL, status TEXT NOT NULL, channel TEXT NOT NULL,
                     shipping_fee REAL NOT NULL);
CREATE TABLE order_items (item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders,
                          product_id INTEGER NOT NULL REFERENCES products, quantity INTEGER NOT NULL,
                          unit_price REAL NOT NULL);
CREATE TABLE refunds (refund_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders,
                      refund_date TEXT NOT NULL, amount REAL NOT NULL);
"""

REGIONS = ["north", "south", "east", "west"]
CATEGORIES = ["books", "games", "garden", "kitchen", "toys"]


def generate(conn: sqlite3.Connection, customers: int = 200, orders: int = 3000, seed: int = 7) -> dict:
    rng = random.Random(seed)
    conn.executescript(SCHEMA)
    start = date(2025, 1, 1)
    cust_rows = []
    for cid in range(1, customers + 1):
        region = None if rng.random() < 0.03 else rng.choice(REGIONS)  # a few unknown regions
        cust_rows.append((cid, region, rng.choice(["consumer", "consumer", "business"]),
                          (start - timedelta(days=rng.randrange(0, 700))).isoformat()))
    prod_rows = [(pid, rng.choice(CATEGORIES), round(rng.uniform(2, 40), 2)) for pid in range(1, 41)]
    order_rows, item_rows, refund_rows = [], [], []
    item_id = refund_id = 0
    for oid in range(1, orders + 1):
        odate = start + timedelta(days=rng.randrange(0, 365))
        status = rng.choices(["completed", "cancelled", "returned"], [85, 8, 7])[0]
        order_rows.append((oid, rng.randrange(1, customers + 1), odate.isoformat(), status,
                           rng.choice(["web", "web", "app", "store"]), rng.choice([0.0, 4.99, 9.99])))
        total = 0.0
        for _ in range(rng.randint(1, 5)):
            item_id += 1
            pid = rng.randrange(1, 41)
            price = round(prod_rows[pid - 1][2] * rng.uniform(1.2, 2.5), 2)
            qty = rng.randint(1, 4)
            item_rows.append((item_id, oid, pid, qty, price))
            total += qty * price
        n_refunds = 2 if status == "returned" and rng.random() < 0.4 else int(status == "returned" or rng.random() < 0.03)
        for _ in range(n_refunds):
            refund_id += 1
            rdate = odate + timedelta(days=rng.randint(3, 45))  # may land in a later month / 2026
            refund_rows.append((refund_id, oid, rdate.isoformat(), round(total * rng.uniform(0.1, 0.5), 2)))
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", cust_rows)
    conn.executemany("INSERT INTO products VALUES (?, ?, ?)", prod_rows)
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", order_rows)
    conn.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", item_rows)
    conn.executemany("INSERT INTO refunds VALUES (?, ?, ?, ?)", refund_rows)
    conn.commit()
    return {"customers": len(cust_rows), "products": len(prod_rows), "orders": len(order_rows),
            "order_items": len(item_rows), "refunds": len(refund_rows)}
