"""Compare metricsql and the join-everything baseline against the pure-Python oracle.

For each query, every (group, metric) cell is checked against metricsql.reference. A cell is
wrong if it differs by more than 1e-6 relative error. Also times compile + execute.

    python benchmark.py [--seed 7] [--orders 3000]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from metricsql import SemanticModel, compile_query
from metricsql.naive import compile_naive
from metricsql.reference import compute
from metricsql.synth import generate

MODEL = Path(__file__).resolve().parent / "samples" / "model.json"
QUERIES = {
    "orders+items by region": {"metrics": ["revenue", "orders", "aov"], "group_by": ["customers__region"]},
    "orders+items+refunds by month": {"metrics": ["revenue", "shipping_revenue", "refunds", "net_revenue"],
                                      "group_by": ["metric_time__month"]},
    "distinct buyers by channel": {"metrics": ["buyers", "orders", "units"], "group_by": ["orders__channel"]},
    "filtered, region x month": {"metrics": ["completed_revenue", "refund_count", "orders"],
                                 "group_by": ["customers__region", "metric_time__month"],
                                 "where": [{"dimension": "orders__channel", "op": "in", "value": ["web", "app"]}]},
}


def close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-6 * max(1.0, abs(b))


def score(conn, compiled, query) -> dict:
    t0 = time.perf_counter()
    rows = conn.execute(compiled.sql, compiled.params).fetchall()
    elapsed = time.perf_counter() - t0
    channel = next((f["value"] for f in query.get("where", []) if f["dimension"] == "orders__channel"), None)
    truth = compute(conn, query["metrics"], query["group_by"], channel)
    ndim = len(query["group_by"])
    got = {tuple(r[:ndim]): dict(zip(query["metrics"], r[ndim:])) for r in rows}
    zero = {m: (None if m == "aov" else 0) for m in query["metrics"]}
    cells = wrong = 0
    worst = 0.0
    for key in set(truth) | set(got):
        for m in query["metrics"]:
            want, have = truth.get(key, zero)[m], got.get(key, zero)[m]
            cells += 1
            if not close(have, want):
                wrong += 1
                if want:
                    worst = max(worst, abs(have - want) / abs(want))
    return {"cells": cells, "wrong": wrong, "worst_rel_error": round(worst, 3), "ms": round(elapsed * 1000, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--orders", type=int, default=3000)
    args = ap.parse_args()
    conn = sqlite3.connect(":memory:")
    counts = generate(conn, orders=args.orders, seed=args.seed)
    model = SemanticModel.load(MODEL)
    print(json.dumps(counts))
    print(f"{'query':<32} {'method':<10} {'cells':>6} {'wrong':>6} {'worst rel err':>14} {'ms':>8}")
    for name, q in QUERIES.items():
        for method, fn in (("metricsql", compile_query), ("naive", compile_naive)):
            r = score(conn, fn(model, q), q)
            print(f"{name:<32} {method:<10} {r['cells']:>6} {r['wrong']:>6} {r['worst_rel_error']:>14} {r['ms']:>8}")


if __name__ == "__main__":
    main()
