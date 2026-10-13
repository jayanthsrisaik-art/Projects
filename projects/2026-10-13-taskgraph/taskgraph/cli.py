"""Command line interface: list | graph | run."""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load
from .graph import levels, select, validate
from .runner import FAILED, SKIPPED, run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="taskgraph", description="Dependency-aware cached task runner")
    p.add_argument("-f", "--file", default="tasks.toml", help="task file (default: tasks.toml)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list tasks")
    g = sub.add_parser("graph", help="show execution waves")
    g.add_argument("targets", nargs="*")
    r = sub.add_parser("run", help="run tasks (and their dependencies)")
    r.add_argument("targets", nargs="*")
    r.add_argument("-j", "--jobs", type=int, default=4)
    r.add_argument("--force", action="store_true", help="ignore the cache")
    r.add_argument("--dry-run", action="store_true", help="show what would run")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project = load(args.file)
        validate(project)
        if args.command == "list":
            for t in project.tasks.values():
                deps = f"  (after: {', '.join(t.deps)})" if t.deps else ""
                print(f"{t.name}{deps}  {t.description}".rstrip())
            return 0
        if args.command == "graph":
            for i, wave in enumerate(levels(project, select(project, args.targets))):
                print(f"wave {i}: {', '.join(wave)}")
            return 0
        results = run(project, args.targets, jobs=args.jobs, force=args.force, dry_run=args.dry_run)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    exit_code = 0
    for wave in levels(project, set(results)):
        for name in wave:
            r = results[name]
            extra = f" ({r.reason})" if r.reason else ""
            print(f"[{r.status:>7}] {name}{extra}")
            if r.status == FAILED:
                exit_code = 1
                print("          " + r.output.strip().replace("\n", "\n          "))
            elif r.status == SKIPPED:
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
