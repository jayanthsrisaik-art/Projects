from __future__ import annotations

import argparse
import sqlite3
import sys

from .project import ProjectError, build_order, load_project
from .runner import run, run_tests


def lineage(project) -> str:
    lines = []
    for name in build_order(project):
        m = project.models[name]
        lines.append(f"{name} [{m.materialized}] <- {', '.join(m.deps) or '-'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="minidbt")
    ap.add_argument("command", choices=["run", "test", "build", "lineage"])
    ap.add_argument("--project", default="example")
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--select")
    ap.add_argument("--full-refresh", action="store_true")
    a = ap.parse_args(argv)
    try:
        proj = load_project(a.project)
        if a.command == "lineage":
            print(lineage(proj))
            return 0
        conn = sqlite3.connect(a.db)
        bad = False
        if a.command in ("run", "build"):
            for r in run(conn, proj, a.select, a.full_refresh):
                print(f"{r.status.upper():8} {r.name}  {r.detail}")
                bad |= r.status in ("error", "skipped")
        if a.command in ("test", "build") and not bad:
            for r in run_tests(conn, proj):
                print(f"{r.status.upper():8} {r.name}  {r.detail}")
                bad |= r.status == "fail"
        return 1 if bad else 0
    except ProjectError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
