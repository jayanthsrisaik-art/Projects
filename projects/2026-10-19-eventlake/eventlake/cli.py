"""Command line: `python -m eventlake ingest|compact|query`."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from .contract import Contract
from .ingest import compact, ingest, read_partition


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eventlake")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("--contract", required=True)
    i.add_argument("--lake", required=True)
    i.add_argument("--state", default=None, help="state db (default: <lake>/_state.db)")
    i.add_argument("--lateness-hours", type=float, default=24)
    i.add_argument("sources", nargs="+")
    c = sub.add_parser("compact")
    c.add_argument("--lake", required=True)
    c.add_argument("--dataset", required=True)
    q = sub.add_parser("query")
    q.add_argument("--lake", required=True)
    q.add_argument("--dataset", required=True)
    q.add_argument("--partition", required=True, help="e.g. dt=2026-01-01")
    a = p.parse_args(argv)

    if a.cmd == "ingest":
        Path(a.lake).mkdir(parents=True, exist_ok=True)
        rep = ingest(Contract.load(a.contract), a.sources, a.lake,
                     a.state or str(Path(a.lake) / "_state.db"),
                     timedelta(hours=a.lateness_hours))
        print(json.dumps(rep.to_dict(), indent=2, sort_keys=True))
    elif a.cmd == "compact":
        print(json.dumps(compact(a.lake, a.dataset), indent=2, sort_keys=True))
    else:
        for r in sorted(read_partition(a.lake, a.dataset, a.partition),
                        key=lambda r: json.dumps(r, sort_keys=True)):
            print(json.dumps(r, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
