"""Deterministic benchmark: greedy vs chronological backtracking vs backjumping.

Reports counts (candidates tried, solutions found) rather than wall time so the
output is reproducible. Usage: python bench.py [--seeds 1000]
"""

from __future__ import annotations

import argparse
import random

from depsolve.greedy import greedy_resolve
from depsolve.oracle import brute_force_satisfiable
from depsolve.resolver import Resolver, verify
from depsolve.synth import backjump_problem, random_problem


def random_suite(seeds: int) -> list[str]:
    sat = greedy_ok = cbj_ok = chrono_ok = invalid = 0
    cbj_steps = chrono_steps = 0
    for seed in range(seeds):
        reg, root, locked = random_problem(random.Random(seed))
        expected = brute_force_satisfiable(reg, root, locked)
        sat += expected
        g = greedy_resolve(reg, root, locked=locked)
        c = Resolver(reg, root, locked=locked).resolve()
        h = Resolver(reg, root, locked=locked, backjump=False).resolve()
        greedy_ok += g.ok
        cbj_ok += c.ok and expected
        chrono_ok += h.ok and expected
        invalid += bool(c.ok and verify(reg, root, c.solution, locked))
        cbj_steps += c.stats.steps
        chrono_steps += h.stats.steps
    return [
        f"## Random registries ({seeds} seeds, 3-5 packages, exhaustive oracle)",
        "",
        "| strategy | solvable found | candidates tried |",
        "|---|---:|---:|",
        f"| oracle (brute force) | {sat}/{sat} | - |",
        f"| greedy, no backtracking | {greedy_ok}/{sat} | - |",
        f"| chronological backtracking | {chrono_ok}/{sat} | {chrono_steps} |",
        f"| backjumping (default) | {cbj_ok}/{sat} | {cbj_steps} |",
        "",
        f"Unsatisfiable seeds: {seeds - sat}; invalid solutions returned by the resolver: {invalid}.",
    ]


def scaling_suite(ks: range) -> list[str]:
    lines = [
        "## Deep conflict (a@2 -> q -> w ^2 vs root w ^1, k independent 4-version packages)",
        "",
        "| k | chronological candidates | backjumping candidates | backjumps |",
        "|---:|---:|---:|---:|",
    ]
    for k in ks:
        reg, root = backjump_problem(k)
        h = Resolver(reg, root, backjump=False, max_steps=10_000_000).resolve()
        c = Resolver(reg, root).resolve()
        assert h.ok and c.ok and h.solution == c.solution
        lines.append(f"| {k} | {h.stats.steps} | {c.stats.steps} | {c.stats.backjumps} |")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=1000)
    args = ap.parse_args()
    print("\n".join(random_suite(args.seeds) + [""] + scaling_suite(range(1, 8))))


if __name__ == "__main__":
    main()
