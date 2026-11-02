"""Command line: generate | ingest | compact | query | check | bench."""
from __future__ import annotations

import argparse
import json
import sys

from . import bench, store, synth
from .events import parse_ts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sketchroll", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write a seeded synthetic dataset (daily JSONL files + late.jsonl)")
    g.add_argument("--out", required=True)
    g.add_argument("--days", type=int, default=14)
    g.add_argument("--events-per-day", type=int, default=15000)
    g.add_argument("--seed", type=int, default=7)

    i = sub.add_parser("ingest", help="ingest JSONL batches (idempotent per file content)")
    i.add_argument("--db", required=True)
    i.add_argument("files", nargs="+")

    c = sub.add_parser("compact", help="fold hourly rows of days ending before --before into day rows")
    c.add_argument("--db", required=True)
    c.add_argument("--before", required=True, help="ISO timestamp, e.g. 2026-10-13T00:00:00Z")

    q = sub.add_parser("query", help="summarise [start, end) from the rollups")
    q.add_argument("--db", required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--end", required=True)
    q.add_argument("--endpoint", action="append", help="repeatable; default: all endpoints")

    k = sub.add_parser("check", help="verify store invariants")
    k.add_argument("--db", required=True)

    b = sub.add_parser("bench", help="end-to-end accuracy benchmark vs exact and naive rollups")
    b.add_argument("--days", type=int, default=14)
    b.add_argument("--events-per-day", type=int, default=15000)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--json", action="store_true")

    a = p.parse_args(argv)
    if a.cmd == "generate":
        meta = synth.write_dataset(a.out, days=a.days, events_per_day=a.events_per_day, seed=a.seed)
        print(json.dumps({**meta, "incidents": [list(x) for x in meta["incidents"]]}, indent=2))
    elif a.cmd == "ingest":
        conn = store.connect(a.db)
        for f in a.files:
            try:
                r = store.ingest_file(conn, f)
            except store.BatchConflict as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(json.dumps({"file": f, "status": r.status, "accepted": r.accepted,
                              "rejected": r.rejected, "cells_touched": r.cells_touched}))
    elif a.cmd == "compact":
        n = store.compact(store.connect(a.db), parse_ts(a.before))
        print(json.dumps({"day_rows_written": n}))
    elif a.cmd == "query":
        try:
            r = store.query(store.connect(a.db), parse_ts(a.start), parse_ts(a.end), a.endpoint)
        except (store.GranularityError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"start": a.start, "end": a.end, "endpoints": a.endpoint or "ALL", **r.summary()}, indent=2))
    elif a.cmd == "check":
        problems = store.check_invariants(store.connect(a.db))
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 1 if problems else 0
    elif a.cmd == "bench":
        r = bench.run(days=a.days, events_per_day=a.events_per_day, seed=a.seed)
        print(json.dumps(r, indent=2, default=str) if a.json else bench.render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
