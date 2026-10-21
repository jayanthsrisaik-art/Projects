"""Deterministic comparison of the algorithms under two workloads.

Run: python -m ratelimit.simulate
"""
from __future__ import annotations

from .algorithms import ALGORITHMS, make_limiter

LIMIT, WINDOW = 10, 10.0


def boundary_burst():
    """10 requests just before t=10 and 10 just after: a fixed window sees two 'fresh' windows."""
    return [9.0 + i * 0.09 for i in range(10)] + [10.0 + i * 0.09 for i in range(10)]


def steady_overload():
    """3 requests/second for 30 seconds against a 1 request/second budget."""
    return [i / 3 for i in range(90)]


WORKLOADS = {"boundary_burst": boundary_burst, "steady_overload": steady_overload}


def worst_window(times: list[float], window: float) -> int:
    """Max accepted requests inside any rolling window of `window` seconds."""
    best = 0
    for i, t in enumerate(times):
        best = max(best, sum(1 for u in times[i:] if u < t + window))
    return best


def run() -> dict:
    out: dict = {}
    for wname, gen in WORKLOADS.items():
        out[wname] = {}
        for aname in ALGORITHMS:
            lim = make_limiter(aname, LIMIT, WINDOW)
            accepted = [t for t in gen() if lim.check("c", t).allowed]
            out[wname][aname] = {"accepted": len(accepted), "max_in_any_10s": worst_window(accepted, WINDOW)}
    return out


def main():
    print(f"limit={LIMIT} per {WINDOW:g}s")
    for wname, rows in run().items():
        print(f"\n{wname}")
        print(f"  {'algorithm':<16}{'accepted':>9}{'max in any 10s':>16}")
        for aname, r in rows.items():
            print(f"  {aname:<16}{r['accepted']:>9}{r['max_in_any_10s']:>16}")


if __name__ == "__main__":
    main()
