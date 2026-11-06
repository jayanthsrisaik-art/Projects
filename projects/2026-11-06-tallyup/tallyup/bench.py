"""Measured comparisons: settle-up transfer counts and rounding drift.

    python -m tallyup.bench            # prints a markdown report (seeded, deterministic counts)
"""
from __future__ import annotations

import argparse
import random
import statistics
import time
from fractions import Fraction

from .money import allocate, rounding_bias
from .settle import apply, greedy_plan, optimal_plan


def random_balances(rng: random.Random, n: int, structured: bool) -> dict[int, int]:
    """Balances that come from real expenses: each expense has a payer and a subset of participants."""
    bal = {i: 0 for i in range(n)}
    if structured:
        # Sub-groups that mostly spend among themselves (car 1 / car 2, flat A / flat B).
        cuts = sorted(rng.sample(range(1, n), k=min(n - 1, max(1, n // 3))))
        clusters, start = [], 0
        for c in cuts + [n]:
            clusters.append(list(range(start, c)))
            start = c
        clusters = [c for c in clusters if len(c) >= 2] or [list(range(n))]
    else:
        clusters = [list(range(n))]
    for _ in range(rng.randint(n, 3 * n)):
        members = rng.choice(clusters)
        payer = rng.choice(members)
        parts = rng.sample(members, k=rng.randint(2, len(members))) if len(members) > 1 else members
        total = rng.randint(100, 20000)
        shares = allocate(total, [1] * len(parts))
        bal[payer] += total
        for m, s in zip(parts, shares):
            bal[m] -= s
    return bal


def settle_report(seed: int, trials: int) -> list[str]:
    rng = random.Random(seed)
    lines = ["| members | balances | trials | greedy mean | optimal mean | optimal < greedy | max saved |",
             "|---:|---|---:|---:|---:|---:|---:|"]
    for structured in (False, True):
        for n in (4, 6, 8, 10, 12):
            g_counts, o_counts, better, saved = [], [], 0, 0
            for _ in range(trials):
                bal = random_balances(rng, n, structured)
                g, o = greedy_plan(bal), optimal_plan(bal)
                for p in (g, o):
                    assert all(v == 0 for v in apply(bal, p.transfers).values())
                g_counts.append(len(g.transfers))
                o_counts.append(len(o.transfers))
                better += len(o.transfers) < len(g.transfers)
                saved = max(saved, len(g.transfers) - len(o.transfers))
            lines.append(f"| {n} | {'sub-groups' if structured else 'mixed'} | {trials} | "
                         f"{statistics.mean(g_counts):.2f} | {statistics.mean(o_counts):.2f} | "
                         f"{100 * better / trials:.1f}% | {saved} |")
    return lines


def drift_report(seed: int, expenses: int) -> list[str]:
    lines = ["| members | expenses | fixed-order max abs drift (minor units) | drift-aware max abs drift |",
             "|---:|---:|---:|---:|"]
    for n in (3, 4, 6):
        result = {}
        for mode in ("fixed", "aware"):
            rng = random.Random(seed + n)  # same expense stream for both modes
            bias = [Fraction(0)] * n
            worst = Fraction(0)
            for _ in range(expenses):
                k = rng.randint(2, n)
                idx = sorted(rng.sample(range(n), k))
                weights = [rng.choice([1, 1, 1, 2, 3]) for _ in idx]
                total = rng.randint(100, 10000)
                parts = allocate(total, weights, [bias[i] for i in idx] if mode == "aware" else None)
                for i, b in zip(idx, rounding_bias(parts, total, weights)):
                    bias[i] += b
                worst = max(worst, max(abs(b) for b in bias))
            result[mode] = worst
        lines.append(f"| {n} | {expenses} | {float(result['fixed']):.2f} | {float(result['aware']):.2f} |")
    return lines


def timing_report(seed: int) -> list[str]:
    rng = random.Random(seed)
    lines = ["| members | optimal plan time (ms, median of 5) |", "|---:|---:|"]
    for n in (8, 12, 16):
        times = []
        for _ in range(5):
            bal = random_balances(rng, n, False)
            t0 = time.perf_counter()
            optimal_plan(bal)
            times.append((time.perf_counter() - t0) * 1000)
        lines.append(f"| {n} | {statistics.median(times):.1f} |")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--expenses", type=int, default=2000)
    ap.add_argument("--no-timing", action="store_true", help="omit wall-clock timings (for exact diffs)")
    a = ap.parse_args()
    out = ["## Settle-up: transfers needed", "", *settle_report(a.seed, a.trials), "",
           "## Rounding drift over a stream of expenses", "", *drift_report(a.seed, a.expenses)]
    if not a.no_timing:
        out += ["", "## Optimal planner cost", "", *timing_report(a.seed)]
    print("\n".join(out))


if __name__ == "__main__":
    main()
