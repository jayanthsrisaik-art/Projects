"""Command line: ``diff``, ``apply``, ``merge`` and ``bench``."""

from __future__ import annotations

import argparse
import sys

from .bench import format_scores, run_bench
from .merge3 import STYLES, merge3
from .myers import MATCHERS
from .unified import PatchError, apply_patch, split_lines, unified_diff


def _read(path: str) -> list[str]:
    with open(path, encoding="utf-8", newline="") as fh:
        return split_lines(fh.read())


def _write(path: str | None, text: str) -> None:
    if path is None or path == "-":
        sys.stdout.write(text)
    else:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def cmd_diff(args) -> int:
    patch = unified_diff(_read(args.old), _read(args.new), args.old, args.new, args.unified, MATCHERS[args.matcher])
    sys.stdout.write(patch)
    return 1 if patch else 0


def cmd_apply(args) -> int:
    with open(args.patch, encoding="utf-8", newline="") as fh:
        patch = fh.read()
    try:
        result = apply_patch(_read(args.file), patch)
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for k, off in enumerate(result.offsets, 1):
        if off:
            print(f"hunk #{k} applied at offset {off:+d}", file=sys.stderr)
    _write(args.output, "".join(result.lines))
    return 0


def cmd_merge(args) -> int:
    labels = tuple(args.label + [args.ours, args.base, args.theirs][len(args.label) :])
    result = merge3(
        _read(args.base), _read(args.ours), _read(args.theirs), args.style, labels, MATCHERS[args.matcher]
    )
    _write(args.output, result.text)
    if result.conflicts:
        print(f"{result.conflicts} conflict(s)", file=sys.stderr)
    return min(result.conflicts, 127)


def cmd_bench(args) -> int:
    print(format_scores(run_bench(args.trials, args.seed, not args.no_git)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="threeway", description="Myers diff, patch and diff3 merge.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diff", help="unified diff of two files (exit 1 if they differ)")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("-U", "--unified", type=int, default=3, help="context lines (default 3)")
    d.add_argument("--matcher", choices=sorted(MATCHERS), default="myers")
    d.set_defaults(func=cmd_diff)

    a = sub.add_parser("apply", help="apply a unified diff to a file")
    a.add_argument("file")
    a.add_argument("patch")
    a.add_argument("-o", "--output", help="write here instead of stdout")
    a.set_defaults(func=cmd_apply)

    m = sub.add_parser("merge", help="three-way merge, git merge-file argument order (exit = #conflicts)")
    m.add_argument("ours")
    m.add_argument("base")
    m.add_argument("theirs")
    m.add_argument("--style", choices=STYLES, default="merge")
    m.add_argument("-L", "--label", action="append", default=[], help="marker label (repeat up to 3x)")
    m.add_argument("--matcher", choices=sorted(MATCHERS), default="myers")
    m.add_argument("-o", "--output", help="write here instead of stdout")
    m.set_defaults(func=cmd_merge)

    b = sub.add_parser("bench", help="synthetic ground-truth merge benchmark")
    b.add_argument("--trials", type=int, default=300)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--no-git", action="store_true", help="skip git merge-file comparison")
    b.set_defaults(func=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "label", None) and len(args.label) > 3:
        print("error: at most three -L labels", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
