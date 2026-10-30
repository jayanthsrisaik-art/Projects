"""Run the server: python -m slotwise [--db PATH] [--seed FILE] [--port N]."""
import argparse
from pathlib import Path

import uvicorn

from .app import create_app

DEFAULT_SEED = Path(__file__).resolve().parent.parent / "samples" / "event_types.json"


def main() -> None:
    p = argparse.ArgumentParser(prog="slotwise")
    p.add_argument("--db", default="slotwise.db")
    p.add_argument("--seed", default=str(DEFAULT_SEED))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()
    uvicorn.run(create_app(a.db, seed_file=a.seed), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
