"""Run the server: ``python -m flagdeck [--db PATH] [--port N] [--seed samples/flags.json]``."""
import argparse
import os

import uvicorn

from .seed import seed


def main() -> None:
    ap = argparse.ArgumentParser(prog="flagdeck")
    ap.add_argument("--db", default=os.environ.get("FLAGDECK_DB", "flagdeck.db"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--seed", help="JSON file of flags to create if they don't exist yet")
    args = ap.parse_args()
    os.environ["FLAGDECK_DB"] = args.db
    if args.seed:
        print(seed(args.db, args.seed))
    uvicorn.run("flagdeck.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
