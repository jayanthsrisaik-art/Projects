"""Measure correctness of the engine and the naive baseline against the
brute-force oracle over a full calendar year, plus engine vs oracle speed.

    python -m cronwhen.bench [--year 2026] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .baseline import naive_fires
from .oracle import brute_force_fires
from .parser import parse
from .schedule import Schedule

JOBS = {
    "daily-0230": "30 2 * * *",
    "daily-0130": "30 1 * * *",
    "every-15m": "*/15 * * * *",
    "hourly-45": "45 * * * *",
    "night-5m": "*/5 0-3 * * *",
    "sun-0200": "0 2 * * SUN",
    "month-end": "0 1 L * *",
}
ZONES = ["America/New_York", "Europe/London", "Australia/Lord_Howe", "America/Santiago"]


def _diff(got, want):
    g, w = Counter(got), Counter(want)
    missing = sum((w - g).values())
    extra = sum((g - w).values())
    return missing, extra


def run(year: int):
    start, end = datetime(year, 1, 1), datetime(year + 1, 1, 1)
    rows = []
    totals = {"engine": [0, 0, 0], "naive": [0, 0, 0]}
    t_engine = t_oracle = 0.0
    oracle_total = 0
    for zone in ZONES:
        tz = ZoneInfo(zone)
        s_aw, e_aw = start.replace(tzinfo=tz), end.replace(tzinfo=tz)
        for name, expr in JOBS.items():
            spec = parse(expr)
            t0 = time.perf_counter()
            want = brute_force_fires(spec, tz, s_aw, e_aw)
            t1 = time.perf_counter()
            got = [o.instant for o in Schedule(spec, tz).fires(s_aw, e_aw)]
            t2 = time.perf_counter()
            naive = naive_fires(spec, tz, s_aw, e_aw)
            t_oracle += t1 - t0
            t_engine += t2 - t1
            oracle_total += len(want)
            em, ee = _diff(got, want)
            nm, ne = _diff(naive, want)
            dup = len(naive) - len(set(naive))
            totals["engine"][0] += em
            totals["engine"][1] += ee
            totals["naive"][0] += nm
            totals["naive"][1] += ne
            totals["naive"][2] += dup
            rows.append({"zone": zone, "job": name, "expr": expr, "oracle_fires": len(want),
                         "engine_missing": em, "engine_extra": ee,
                         "naive_missing": nm, "naive_extra": ne, "naive_duplicates": dup})
    return {"year": year, "pairs": len(rows), "oracle_fires": oracle_total,
            "engine": {"missing": totals["engine"][0], "extra": totals["engine"][1]},
            "naive": {"missing": totals["naive"][0], "extra": totals["naive"][1],
                      "duplicate_instants": totals["naive"][2],
                      "pairs_wrong": sum(1 for r in rows if r["naive_missing"] or r["naive_extra"])},
            "seconds": {"engine": round(t_engine, 3), "oracle": round(t_oracle, 3)},
            "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = run(a.year)
    if a.json:
        json.dump(res, sys.stdout, indent=2)
        print()
        return 0
    print(f"year {res['year']}: {res['pairs']} job x zone pairs, {res['oracle_fires']} oracle fires")
    print(f"{'zone':<20} {'job':<11} {'fires':>6} {'engine -/+':>11} {'naive -/+':>10} {'naive dup':>9}")
    for r in res["rows"]:
        if r["naive_missing"] or r["naive_extra"] or r["engine_missing"] or r["engine_extra"]:
            print(f"{r['zone']:<20} {r['job']:<11} {r['oracle_fires']:>6} "
                  f"{r['engine_missing']:>5}/{r['engine_extra']:<5} {r['naive_missing']:>4}/"
                  f"{r['naive_extra']:<5} {r['naive_duplicates']:>9}")
    print(f"engine: missing={res['engine']['missing']} extra={res['engine']['extra']}")
    n = res["naive"]
    print(f"naive : missing={n['missing']} extra={n['extra']} duplicate_instants="
          f"{n['duplicate_instants']} pairs_wrong={n['pairs_wrong']}/{res['pairs']}")
    print(f"time  : engine {res['seconds']['engine']}s vs minute-scan oracle {res['seconds']['oracle']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
