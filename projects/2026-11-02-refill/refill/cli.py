"""Command-line interface: ``python -m refill {lint,plan,demo}``."""
from __future__ import annotations

import argparse
import json
import sys

from .planner import explain, plan
from .spec import SpecError, load_pipeline, parse_change


def _compress(keys: list[str]) -> str:
    """Render sorted day keys as ranges: 2026-10-01..2026-10-07, 2026-10-09."""
    if not keys or len(keys[0]) != 10:
        return ", ".join(keys)
    from datetime import date, timedelta

    out, start, prev = [], keys[0], keys[0]
    for k in keys[1:] + [None]:
        if k is not None and date.fromisoformat(k) == date.fromisoformat(prev) + timedelta(days=1):
            prev = k
            continue
        out.append(start if start == prev else f"{start}..{prev}")
        if k is not None:
            start = prev = k
    return ", ".join(out)


def cmd_lint(args) -> int:
    p = load_pipeline(args.spec)
    print(f"OK: {len(p.tables)} tables, topo order: {' -> '.join(p.order)}")
    for t in p.order:
        tb = p.tables[t]
        deps = ", ".join(f"{d.table}[{d.mapping.describe()}]" for d in tb.deps) or "(source)"
        print(f"  {t:<22} {tb.grain:<6} {deps}")
    return 0


def cmd_plan(args) -> int:
    p = load_pipeline(args.spec)
    roots = [n for c in args.change for n in parse_change(p, c)]
    pl = plan(p, roots)
    if args.json:
        print(json.dumps({
            "roots": [list(r) for r in pl.roots],
            "tasks": [{"table": t, "partition": k, "wave": pl.level[(t, k)],
                       "because": list(pl.reason[(t, k)]) if (t, k) in pl.reason else None} for t, k in pl.tasks],
            "total_tasks": len(pl.tasks),
            "waves": pl.waves,
        }, indent=2))
        return 0
    total = len(p.all_nodes(derived_only=True))
    print(f"roots: {len(pl.roots)} partition(s)")
    print(f"plan: {len(pl.tasks)} of {total} derived partitions, {pl.waves} wave(s)")
    for t, keys in pl.by_table().items():
        print(f"  {t:<22} {len(keys):>4}  {_compress(keys)}")
    for node_text in args.explain or []:
        t, k = node_text.split(":", 1)
        if (t, k) not in pl.level:
            print(f"why {t}:{k}: not in plan")
            continue
        chain = " <- ".join(f"{a}:{b}" for a, b in reversed(explain(pl, (t, k))))
        print(f"why {t}:{k}: {chain}")
    return 0


def cmd_demo(args) -> int:
    from .demo import format_report, run_demo

    rep = run_demo(load_pipeline(args.spec), seed=args.seed)
    if args.json:
        for sc in rep["scenarios"]:  # timings are not deterministic
            for r in sc["strategies"].values():
                r.pop("seconds")
        print(json.dumps(rep, indent=2))
    else:
        print(format_report(rep))
    bad = rep["scenarios"] and any(
        sc["strategies"][s]["wrong_partitions"]
        for sc in rep["scenarios"] for s in ("refill", "refill_cutoff")
    )
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="refill", description="Partition-level backfill planner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lint", help="validate a pipeline spec")
    a.add_argument("spec")
    a.set_defaults(fn=cmd_lint)
    a = sub.add_parser("plan", help="plan the minimal backfill for changed partitions")
    a.add_argument("spec")
    a.add_argument("--change", "-c", action="append", required=True,
                   help="table:key | table:start..end | table:*  (repeatable)")
    a.add_argument("--explain", "-e", action="append", metavar="TABLE:KEY",
                   help="print the invalidation chain for a planned partition")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_plan)
    a = sub.add_parser("demo", help="measure strategies against a full-rebuild oracle")
    a.add_argument("spec")
    a.add_argument("--seed", type=int, default=7)
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_demo)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except SpecError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
