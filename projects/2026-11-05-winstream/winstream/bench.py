"""Latency / completeness trade-off of watermark delay and allowed lateness.

For every configuration the operator runs in memory over the same stream and
is scored against ``oracle.truth`` (GROUP BY over every unique event):

* first_exact  - share of true windows whose FIRST emitted result is already
                 exactly right (what a consumer that never reads updates sees)
* final_exact  - share of true windows whose final materialised result is right
* spurious     - final windows that do not exist in the truth (e.g. a session
                 split in two because the bridging event was dropped)
* dropped      - unique events with at least one (event, window) pair dropped
* corrections  - update + retract rows emitted after the first results
* p50/p95 lat  - processing-time seconds from window end to its first result
                 (trigger event's arrival - window end; flushes excluded)
* peak_state   - max open windows held by the operator

Baseline ``processing-time`` assigns windows by arrival time (delay 0, no
lateness), which is what a consumer that ignores event time computes.
"""
from __future__ import annotations

from .engine import Operator
from .model import Event
from .oracle import truth


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return xs[i]


def score(events, spec, delay, lateness, true=None, processing_time=False):
    true = truth(events, spec) if true is None else true
    stream = events
    if processing_time:
        stream = [Event(e.id, e.key, e.arrival, e.value, e.arrival) for e in events]
    op = Operator(spec, delay, lateness)
    emissions = []
    peak = 0
    late_ids = set()
    for i, e in enumerate(stream):
        out, side = op.process(e, i)
        emissions.extend(out)
        late_ids.update(s["event_id"] for s in side if s["reason"] == "late")
        if i % 50 == 0:
            peak = max(peak, op.open_windows())
    emissions.extend(op.flush())

    first = {}
    final = {}
    lat = []
    for r in emissions:
        k = (r["key"], r["wstart"], r["wend"])
        vals = {c: r[c] for c in ("count", "sum", "min", "max", "mean")}
        if k not in first:
            first[k] = vals if r["kind"] == "insert" else None
            if r["kind"] == "insert" and r["trigger_arrival"] is not None:
                lat.append(r["trigger_arrival"] - r["wend"])
        if r["kind"] == "retract":
            final.pop(k, None)
        else:
            final[k] = vals
    n = len(true)
    return {
        "first_exact": sum(1 for k, v in true.items() if first.get(k) == v) / n,
        "final_exact": sum(1 for k, v in true.items() if final.get(k) == v) / n,
        "spurious": sum(1 for k in final if k not in true),
        "dropped": len(late_ids),
        "corrections": op.stats["updates"] + op.stats["retracts"],
        "p50_latency": _pct(lat, 50),
        "p95_latency": _pct(lat, 95),
        "peak_state": peak,
        "windows": n,
    }


DEFAULT_GRID = [(0, 0), (5, 0), (15, 0), (60, 0), (180, 0), (5, 600), (15, 600), (15, 1800)]


def run_bench(events, specs, grid=DEFAULT_GRID):
    rows = []
    for spec in specs:
        true = truth(events, spec)
        if spec.kind != "session":
            r = score(events, spec, 0, 0, true, processing_time=True)
            rows.append({"window": spec.describe(), "mode": "processing-time", "delay": 0,
                         "lateness": 0, **r})
        for delay, lateness in grid:
            r = score(events, spec, delay, lateness, true)
            rows.append({"window": spec.describe(), "mode": "event-time", "delay": delay,
                         "lateness": lateness, **r})
    return rows


def format_table(rows):
    head = ("window", "mode", "delay", "late", "first_exact", "final_exact", "spurious",
            "dropped", "corrections", "p50_lat", "p95_lat", "peak_state")
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        def f(x):
            return "-" if x is None else str(x)
        lines.append("| " + " | ".join([
            r["window"], r["mode"], str(r["delay"]), str(r["lateness"]),
            f"{100 * r['first_exact']:.1f}%", f"{100 * r['final_exact']:.1f}%",
            str(r["spurious"]), str(r["dropped"]), str(r["corrections"]),
            f(r["p50_latency"]), f(r["p95_latency"]), str(r["peak_state"])]) + " |")
    return "\n".join(lines)
