"""Measure cdcsync vs the naive arrival-order applier on synthetic streams.

    python benchmark.py [--seeds 5] [--transactions 300]

For each reorder window and seed, generates a chaotic stream, applies it with
both appliers and reconciles against the source. Prints a Markdown table of
divergent rows (missing + extra + value mismatches) and cdcsync throughput.
"""

import argparse
import tempfile
import time
from pathlib import Path

from cdcsync import Applier, NaiveApplier, reconcile
from cdcsync.simulator import generate


def divergent(report: dict) -> int:
    return sum(t.get("missing_count", 0) + t.get("extra_count", 0) + t.get("mismatch_count", 0)
               for t in report["tables"].values())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--transactions", type=int, default=300)
    p.add_argument("--windows", default="0,5,20,100")
    args = p.parse_args()
    print("| reorder window | dup rate | runs | naive: runs diverged | naive: divergent rows (mean) "
          "| cdcsync: runs diverged | cdcsync: events/s (median) |")
    print("|---|---|---|---|---|---|---|")
    for window in [int(w) for w in args.windows.split(",")]:
        naive_bad, naive_rows, ours_bad, rates = 0, 0, 0, []
        for seed in range(1, args.seeds + 1):
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                info = generate(d, seed=seed, transactions=args.transactions, window=window, dup_rate=0.05)
                n = NaiveApplier(d / "naive.db")
                n.apply_file(d / "events.jsonl")
                n.close()
                r = reconcile(d / "source.db", d / "naive.db")
                naive_bad += not r["ok"]
                naive_rows += divergent(r)
                a = Applier(d / "t.db")
                t0 = time.perf_counter()
                a.apply_file(d / "events.jsonl")
                rates.append(info["lines"] / (time.perf_counter() - t0))
                a.close()
                ours_bad += not reconcile(d / "source.db", d / "t.db")["ok"]
        rates.sort()
        print(f"| {window} | 0.05 | {args.seeds} | {naive_bad} | {naive_rows / args.seeds:.1f} "
              f"| {ours_bad} | {rates[len(rates) // 2]:,.0f} |")


if __name__ == "__main__":
    main()
