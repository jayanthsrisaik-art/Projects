"""Command line: generate | run | verify | show | bench."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile

from .bench import format_table, run_bench
from .generator import generate
from .model import event_to_json, parse_window
from .pipeline import run, run_until_done
from .sink import CrashInjected, Sink
from .verify import check, load_events


def _common(p):
    p.add_argument("--window", required=True, help="tumbling:S | hopping:S:SLIDE | session:GAP")
    p.add_argument("--delay", type=int, default=15, help="watermark delay (bounded out-of-orderness), s")
    p.add_argument("--lateness", type=int, default=0, help="allowed lateness after the watermark, s")


def cmd_generate(a):
    events = generate(n_keys=a.keys, duration=a.duration, seed=a.seed)
    with open(a.out, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(event_to_json(e) + "\n")
    print(f"wrote {len(events)} events to {a.out}")
    return 0


def cmd_run(a):
    spec = parse_window(a.window)
    try:
        rep = run(a.events, a.db, spec, a.delay, a.lateness, a.checkpoint_every,
                  flush=not a.no_flush, crash_after=a.crash_after)
    except CrashInjected as exc:
        print(f"CRASH: {exc} (re-run the same command to resume from the last checkpoint)")
        return 3
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"read": rep.read, "resumed_from_offset": rep.resumed_from,
                      "commits": rep.commits, "changelog_rows_written": rep.emissions,
                      "flushed": rep.flushed, "open_windows": rep.open_windows,
                      "stats": rep.stats}, indent=2))
    return 0


def cmd_verify(a):
    spec = parse_window(a.window)
    events = load_events(a.events)
    with tempfile.TemporaryDirectory() as td:
        clean = os.path.join(td, "clean.db")
        run(a.events, clean, spec, a.delay, a.lateness, a.checkpoint_every)
        sink = Sink(clean)
        summary = check(sink, events, spec, a.delay, a.lateness)
        clean_log = sink.changelog()
        clean_side = sink.side_outputs()
        sink.close()
        crash_runs = []
        rng = random.Random(a.seed)
        n_lines = sum(1 for _ in open(a.events, encoding="utf-8"))
        for i in range(a.crashes):
            db = os.path.join(td, f"crash{i}.db")
            sched = sorted(rng.randint(1, max(1, n_lines // 3)) for _ in range(3))
            plan = [("after", sched[0]), ("commit", 1), ("after", sched[1]), ("after", sched[2])]
            survived, _ = run_until_done(a.events, db, spec, a.delay, a.lateness,
                                         a.checkpoint_every, crash_schedule=plan)
            s = Sink(db)
            same = s.changelog() == clean_log and s.side_outputs() == clean_side
            s.close()
            crash_runs.append({"crashes": survived, "identical_to_clean_run": same})
        summary["crash_runs"] = crash_runs
        summary["ok"] = summary["ok"] and all(c["identical_to_clean_run"] for c in crash_runs)
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


def cmd_show(a):
    sink = Sink(a.db)
    if a.changelog:
        print("seq\tkind\tkey\twstart\twend\trevision\tcount\tsum\tmin\tmax\tmean\twm\ttrigger_offset")
        for r in sink.changelog():
            print("\t".join("" if r[c] is None else str(r[c]) for c in
                            ("seq", "kind", "key", "wstart", "wend", "revision", "count", "sum",
                             "min", "max", "mean", "wm", "trigger_offset")))
        sink.close()
        return 0
    rows = sink.results()
    if a.key:
        rows = [r for r in rows if r[0] == a.key]
    print("key\twstart\twend\tcount\tsum\tmin\tmax\tmean\trevision")
    for r in rows[: a.limit]:
        print("\t".join("" if v is None else str(v) for v in r))
    late = sink.side_outputs("late")
    print(f"# {len(rows)} windows, {len(sink.changelog())} changelog rows, {len(late)} late pairs, "
          f"{len(sink.side_outputs('duplicate'))} duplicates, {len(sink.side_outputs('invalid'))} invalid")
    sink.close()
    return 0


def cmd_bench(a):
    if a.events:
        events = load_events(a.events)
    else:
        events = generate(n_keys=a.keys, duration=a.duration, seed=a.seed)
    specs = [parse_window(w) for w in a.windows]
    rows = run_bench(events, specs)
    print(f"{len(events)} events (incl. re-deliveries), seed={a.seed}\n")
    print(format_table(rows))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="winstream", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write a synthetic out-of-order stream (JSONL)")
    g.add_argument("--out", required=True)
    g.add_argument("--keys", type=int, default=8)
    g.add_argument("--duration", type=int, default=3 * 3600)
    g.add_argument("--seed", type=int, default=7)
    g.set_defaults(fn=cmd_generate)

    r = sub.add_parser("run", help="process a JSONL stream into a SQLite sink (resumable)")
    r.add_argument("--events", required=True)
    r.add_argument("--db", required=True)
    _common(r)
    r.add_argument("--checkpoint-every", type=int, default=500)
    r.add_argument("--no-flush", action="store_true", help="keep windows open at end of input")
    r.add_argument("--crash-after", type=int, help="simulate a crash after N records (testing)")
    r.set_defaults(fn=cmd_run)

    v = sub.add_parser("verify", help="check a run against the batch oracle, with crash/restart replays")
    v.add_argument("--events", required=True)
    _common(v)
    v.add_argument("--checkpoint-every", type=int, default=200)
    v.add_argument("--crashes", type=int, default=3, help="number of crash/restart replays")
    v.add_argument("--seed", type=int, default=1)
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("show", help="print materialised results")
    s.add_argument("--db", required=True)
    s.add_argument("--key")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--changelog", action="store_true", help="print the changelog instead")
    s.set_defaults(fn=cmd_show)

    b = sub.add_parser("bench", help="latency/completeness trade-off table")
    b.add_argument("--events")
    b.add_argument("--keys", type=int, default=8)
    b.add_argument("--duration", type=int, default=3 * 3600)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--windows", nargs="+", default=["tumbling:60", "hopping:300:60", "session:120"])
    b.add_argument("--json")
    b.set_defaults(fn=cmd_bench)

    a = p.parse_args(argv)
    return a.fn(a)
