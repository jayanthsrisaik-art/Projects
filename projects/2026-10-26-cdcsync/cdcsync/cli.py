"""Command line interface: simulate, apply, status, dump, reconcile."""

from __future__ import annotations

import argparse
import json
import sys

from .applier import Applier, NaiveApplier
from .reconcile import reconcile
from .simulator import generate


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cdcsync", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate", help="generate a synthetic source and chaotic change stream")
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--transactions", type=int, default=300)
    s.add_argument("--window", type=int, default=20, help="max reorder distance (events)")
    s.add_argument("--dup-rate", type=float, default=0.05)
    s.add_argument("--malformed", type=int, default=3)

    a = sub.add_parser("apply", help="apply a JSONL change stream to a target database")
    a.add_argument("events")
    a.add_argument("--target", required=True)
    a.add_argument("--batch-size", type=int, default=500)
    a.add_argument("--source-name", help="offset key (default: file name)")
    a.add_argument("--naive", action="store_true",
                   help="baseline: arrival-order upserts, no LSN guard or tx buffering")

    for name, help_ in (("status", "show offsets, pending transactions and rejects"),
                        ("dump", "print current replicated rows as JSON")):
        x = sub.add_parser(name, help=help_)
        x.add_argument("--target", required=True)

    r = sub.add_parser("reconcile", help="compare source and target; exit 1 on mismatch")
    r.add_argument("--source", required=True)
    r.add_argument("--target", required=True)
    r.add_argument("--buckets", type=int, default=16)

    args = p.parse_args(argv)
    if args.cmd == "simulate":
        _print(generate(args.out, seed=args.seed, transactions=args.transactions,
                        window=args.window, dup_rate=args.dup_rate, malformed=args.malformed))
        return 0
    if args.cmd == "reconcile":
        report = reconcile(args.source, args.target, buckets=args.buckets)
        _print(report)
        return 0 if report["ok"] else 1

    cls = NaiveApplier if getattr(args, "naive", False) else Applier
    applier = cls(args.target)
    try:
        if args.cmd == "apply":
            stats = applier.apply_file(args.events, source=args.source_name, batch_size=args.batch_size)
            _print(stats.as_dict())
        elif args.cmd == "status":
            _print(applier.status())
        elif args.cmd == "dump":
            _print(applier.dump())
    finally:
        applier.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
