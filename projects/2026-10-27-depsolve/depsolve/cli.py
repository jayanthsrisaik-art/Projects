"""Command-line interface: resolve, check, why, match."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .explain import explain
from .lockfile import build_lock, diff_locks, lock_versions, why
from .ranges import RangeError, parse_range
from .registry import Registry, RegistryError, load_manifest
from .resolver import ResolutionError, Resolver, verify
from .semver import VersionError, parse_version


def _cmd_resolve(args: argparse.Namespace) -> int:
    registry = Registry.load(args.registry)
    root_name, root_deps = load_manifest(args.manifest)
    old: dict = {}
    if args.lock and Path(args.lock).exists():
        old = lock_versions(json.loads(Path(args.lock).read_text()))
    upgrade = set(registry.names()) if args.upgrade_all else set(args.upgrade or [])
    resolver = Resolver(
        registry,
        root_deps,
        locked=old,
        upgrade=upgrade,
        backjump=not args.no_backjump,
        max_steps=args.max_steps,
        root_name=root_name,
    )
    try:
        result = resolver.resolve()
    except ResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    s = result.stats
    if not result.ok:
        print(explain(resolver, result).render(), file=sys.stderr)
        print(f"(searched {s.steps} candidates, {s.backjumps} backjumps)", file=sys.stderr)
        return 1
    lock = build_lock(registry, root_name, root_deps, result.solution)
    text = json.dumps(lock, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    print(
        f"resolved {len(result.solution)} packages "
        f"({s.steps} candidates tried, {s.backtracks} backtracks, {s.backjumps} backjumps)",
        file=sys.stderr,
    )
    for line in diff_locks(old, result.solution) if old else []:
        print(f"  {line}", file=sys.stderr)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    registry = Registry.load(args.registry)
    _, root_deps = load_manifest(args.manifest)
    solution = lock_versions(json.loads(Path(args.lock).read_text()))
    problems = verify(registry, root_deps, solution, locked=solution)
    for p in problems:
        print(p)
    if not problems:
        print(f"ok: {len(solution)} locked packages satisfy the manifest and registry")
    return 1 if problems else 0


def _cmd_why(args: argparse.Namespace) -> int:
    chain = why(json.loads(Path(args.lock).read_text()), args.package)
    if chain is None:
        print(f"{args.package} is not in the lockfile", file=sys.stderr)
        return 1
    print(" -> ".join(chain))
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    rng = parse_range(args.range)
    for text in args.versions:
        print(f"{text}\t{'yes' if rng.satisfies(parse_version(text)) else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="depsolve", description="semver dependency resolver")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="resolve a manifest and print/write a lockfile")
    r.add_argument("--registry", required=True)
    r.add_argument("--manifest", required=True)
    r.add_argument("--lock", help="existing lockfile; its versions are preferred")
    r.add_argument("--upgrade", action="append", metavar="PKG", help="ignore the lock for PKG")
    r.add_argument("--upgrade-all", action="store_true")
    r.add_argument("--out", help="write the lockfile here instead of stdout")
    r.add_argument("--no-backjump", action="store_true", help="chronological backtracking")
    r.add_argument("--max-steps", type=int, default=200_000)
    r.set_defaults(fn=_cmd_resolve)

    c = sub.add_parser("check", help="verify a lockfile against manifest and registry")
    c.add_argument("--registry", required=True)
    c.add_argument("--manifest", required=True)
    c.add_argument("--lock", required=True)
    c.set_defaults(fn=_cmd_check)

    w = sub.add_parser("why", help="show the dependency chain that pulls in a package")
    w.add_argument("--lock", required=True)
    w.add_argument("package")
    w.set_defaults(fn=_cmd_why)

    m = sub.add_parser("match", help="test versions against a range")
    m.add_argument("range")
    m.add_argument("versions", nargs="+")
    m.set_defaults(fn=_cmd_match)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (RegistryError, RangeError, VersionError, json.JSONDecodeError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
