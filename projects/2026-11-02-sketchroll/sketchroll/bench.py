"""End-to-end benchmark: generate -> ingest -> compact -> late batch -> query vs truth."""
from __future__ import annotations

import os
import statistics
import tempfile
from datetime import timedelta

from . import store
from .events import read_jsonl
from .oracle import QUANTILES, NaiveRollup, exact
from .rollup import DD_ALPHA
from .synth import ENDPOINTS, write_dataset


def _load(path):
    return [item for _, _, item in read_jsonl(path) if not isinstance(item, Exception)]


def run(days: int = 14, events_per_day: int = 15000, seed: int = 7, keep_hourly_days: int = 2,
        workdir: str | None = None) -> dict:
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory()
        workdir = tmp.name
    try:
        meta = write_dataset(os.path.join(workdir, "data"), days=days, events_per_day=events_per_day, seed=seed)
        conn = store.connect(os.path.join(workdir, "rollups.db"))
        for path in meta["daily_files"]:
            store.ingest_file(conn, path)
        all_events = [e for p in meta["daily_files"] for e in _load(p)]
        first = min(e.ts for e in all_events).replace(hour=0, minute=0, second=0, microsecond=0)
        last_day = first + timedelta(days=days)
        compact_before = last_day - timedelta(days=keep_hourly_days)
        compacted_days = store.compact(conn, compact_before)
        late_result = store.ingest_file(conn, meta["late_file"])  # lands in compacted day rows
        replay = store.ingest_file(conn, meta["late_file"])        # redelivery: must be a no-op
        all_events += _load(meta["late_file"])
        invariants = store.check_invariants(conn)
        naive = NaiveRollup(all_events)

        scopes = [[ep] for ep in ENDPOINTS] + [None]
        windows = [("full range", first, last_day),
                   ("first week", first, first + timedelta(days=7))]
        windows += [(f"day {d + 1}", first + timedelta(days=d), first + timedelta(days=d + 1))
                    for d in range(0, days, max(1, days // 4))]
        recent = last_day - timedelta(hours=6)
        windows += [("last 6 hours", recent, last_day), ("last hour", last_day - timedelta(hours=1), last_day)]

        errs = {"sketch": {}, "naive": {}}
        exact_counts_ok = True
        rows = []
        for label, s, e in windows:
            for scope in scopes:
                truth = exact(all_events, s, e, scope)
                if not truth["requests"]:
                    continue
                sk = store.query(conn, s, e, scope)
                sk_sum = sk.summary(QUANTILES)
                nv = naive.query(s, e, scope)
                exact_counts_ok &= (sk.n == truth["requests"] and sk.errors == truth["errors"]
                                    and abs(sk.lat_sum - truth["latency_sum_ms"]) < 1e-6 * max(1, truth["latency_sum_ms"])
                                    and sk.lat_max == truth["latency_max_ms"])
                for metric in ["distinct_users"] + [f"latency_p{int(q * 100)}_ms" for q in QUANTILES]:
                    t = truth[metric]
                    errs["sketch"].setdefault(metric, []).append(abs(sk_sum[metric] - t) / t)
                    errs["naive"].setdefault(metric, []).append(abs(nv[metric] - t) / t)
                rows.append((label, scope[0] if scope else "ALL", truth["distinct_users"],
                             sk_sum["distinct_users"], nv["distinct_users"],
                             truth["latency_p99_ms"], sk_sum["latency_p99_ms"], nv["latency_p99_ms"]))

        def agg(v):
            return {"median": statistics.median(v), "max": max(v),
                    "within_alpha": sum(x <= DD_ALPHA + 1e-9 for x in v) / len(v)}

        raw_bytes = sum(os.path.getsize(p) for p in meta["daily_files"] + [meta["late_file"]])
        return {
            "events": meta["events"], "late_events": meta["late_events"], "days": days,
            "compacted_day_rows": compacted_days,
            "late_ingest": {"status": late_result.status, "accepted": late_result.accepted,
                            "cells_touched": late_result.cells_touched},
            "late_replay_status": replay.status,
            "invariant_violations": invariants,
            "queries": len(rows),
            "exact_counters_match": exact_counts_ok,
            "errors": {k: {m: agg(v) for m, v in d.items()} for k, d in errs.items()},
            "storage": {"raw_jsonl_bytes": raw_bytes, **store.storage_bytes(conn)},
            "sample_rows": rows,
        }
    finally:
        if tmp is not None:
            conn.close()
            tmp.cleanup()


def render(r: dict) -> str:
    out = [
        f"events={r['events']} (late={r['late_events']}) days={r['days']} "
        f"compacted_day_rows={r['compacted_day_rows']} queries={r['queries']}",
        f"late batch: {r['late_ingest']}  redelivery: {r['late_replay_status']}",
        f"invariant violations: {len(r['invariant_violations'])}   "
        f"exact counters (requests/errors/sum/max) match truth: {r['exact_counters_match']}",
        "",
        "relative error vs exact     |  sketch median   sketch max  |  naive median    naive max",
        "-" * 88,
    ]
    for metric in r["errors"]["sketch"]:
        s, n = r["errors"]["sketch"][metric], r["errors"]["naive"][metric]
        out.append(f"{metric:<27} | {s['median']:>12.2%} {s['max']:>12.2%} | {n['median']:>12.2%} {n['max']:>12.2%}")
    q = [m for m in r["errors"]["sketch"] if m.startswith("latency")]
    out.append("")
    out.append("share of quantile answers within DDSketch alpha=1%: "
               + ", ".join(f"{m}: sketch {r['errors']['sketch'][m]['within_alpha']:.0%} / "
                           f"naive {r['errors']['naive'][m]['within_alpha']:.0%}" for m in q))
    st = r["storage"]
    out.append(f"storage: raw JSONL {st['raw_jsonl_bytes']:,} B -> {st['rollup_rows']} rollup rows, "
               f"HLL {st['hll_bytes']:,} B + DDSketch {st['ddsketch_bytes']:,} B")
    out.append("")
    out.append(f"{'window':<13} {'scope':<10} {'users':>7} {'sketch':>7} {'naive':>7}   "
               f"{'p99':>8} {'sketch':>8} {'naive':>8}")
    for row in r["sample_rows"]:
        if row[1] in ("ALL", "/checkout"):
            out.append(f"{row[0]:<13} {row[1]:<10} {row[2]:>7} {row[3]:>7} {row[4]:>7}   "
                       f"{row[5]:>8.1f} {row[6]:>8.1f} {row[7]:>8.1f}")
    return "\n".join(out)


