"""Command line interface: ``python -m scd2 --db wh.db <command>``."""
from __future__ import annotations

import argparse
import csv
import sys

from .store import SnapshotError, Warehouse


def _emit(rows, out) -> None:
    rows = list(rows)
    w = csv.writer(out, lineterminator="\n")
    if rows:
        w.writerow(rows[0].keys())
        for r in rows:
            w.writerow(["" if v is None else v for v in tuple(r)])


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="scd2", description=__doc__)
    p.add_argument("--db", default="warehouse.db", help="SQLite file (default: warehouse.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ld = sub.add_parser("load", help="load one CSV snapshot and rebuild the dimension")
    ld.add_argument("csv")
    ld.add_argument("--as-of", required=True, help="snapshot date YYYY-MM-DD")
    ld.add_argument("--key", required=True, help="business key column")
    ld.add_argument("--replace", action="store_true", help="overwrite a different snapshot for the same date")

    a = sub.add_parser("as-of", help="dimension state on a date")
    a.add_argument("date")
    h = sub.add_parser("history", help="all versions of one key")
    h.add_argument("key_value")
    sub.add_parser("snapshots", help="list loaded snapshots")
    sub.add_parser("check", help="verify SCD2 invariants (exit 1 on violation)")

    args = p.parse_args(argv)
    wh = Warehouse(args.db)
    try:
        if args.cmd == "load":
            status = wh.load_csv(args.csv, args.as_of, args.key, args.replace)
            print(f"{args.as_of}: {status}", file=out)
        elif args.cmd == "as-of":
            _emit(wh.as_of(args.date), out)
        elif args.cmd == "history":
            _emit(wh.history(args.key_value), out)
        elif args.cmd == "snapshots":
            _emit(wh.snapshots(), out)
        elif args.cmd == "check":
            problems = wh.check()
            if problems:
                for x in problems:
                    print(f"FAIL: {x}", file=out)
                return 1
            print("OK: all invariants hold", file=out)
    except SnapshotError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
