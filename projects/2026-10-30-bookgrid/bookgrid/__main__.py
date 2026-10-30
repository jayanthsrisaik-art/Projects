"""CLI: ``python -m bookgrid {serve,seed,race}``."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .race import run_race
from .seed import load_seed
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bookgrid")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run the API + UI")
    p.add_argument("--db", default="bookgrid.db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("seed", help="load sample rooms and bookings")
    p.add_argument("--db", default="bookgrid.db")
    p.add_argument("--file", default=str(ROOT / "samples" / "seed.json"))
    p = sub.add_parser("race", help="concurrency benchmark: naive vs transactional booking")
    p.add_argument("--trials", type=int, default=20)
    p.add_argument("--clients", type=int, default=8)
    p.add_argument("--pause", type=float, default=0.02)
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(args.db), host=args.host, port=args.port)
    elif args.cmd == "seed":
        print(json.dumps(load_seed(Store(args.db), args.file)))
    else:
        print(json.dumps(run_race(args.trials, args.clients, args.pause), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
