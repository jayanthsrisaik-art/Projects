"""Shared helpers for the cronwhen test-suite (unique module name on purpose)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def transitions(tz, start: datetime, end: datetime):
    """UTC instants where tz's UTC offset changes, found by hourly scan + bisection."""
    out = []
    u = start.replace(tzinfo=UTC)
    end_u = end.replace(tzinfo=UTC)
    step = timedelta(hours=1)
    prev = u.astimezone(tz).utcoffset()
    while u < end_u:
        nxt = u + step
        off = nxt.astimezone(tz).utcoffset()
        if off != prev:
            lo, hi = u, nxt
            while hi - lo > timedelta(seconds=1):
                mid = lo + (hi - lo) / 2
                if mid.astimezone(tz).utcoffset() == prev:
                    lo = mid
                else:
                    hi = mid
            out.append(hi.replace(microsecond=0))
            prev = off
        u = nxt
    return out


def _field(rng: random.Random, lo: int, hi: int) -> str:
    kind = rng.random()
    if kind < 0.25:
        return "*"
    if kind < 0.45:
        return f"*/{rng.choice([2, 3, 5, 7, 10, 15, 20, 30, 45])}"
    if kind < 0.65:
        a = rng.randint(lo, hi)
        b = rng.randint(a, hi)
        return f"{a}-{b}"
    if kind < 0.8:
        a = rng.randint(lo, hi)
        b = rng.randint(a, hi)
        return f"{a}-{b}/{rng.randint(1, 4)}"
    vals = sorted({rng.randint(lo, hi) for _ in range(rng.randint(1, 3))})
    return ",".join(map(str, vals))


def random_expr(rng: random.Random, dst_hours: bool = True) -> str:
    """Random expressions biased towards the 00:00-04:00 hours where DST lives."""
    minute = _field(rng, 0, 59)
    if dst_hours and rng.random() < 0.6:
        hour = rng.choice(["*", "0-3", "1", "2", "3", "1,2", "2-3", "*/2", "0", "*/3"])
    else:
        hour = _field(rng, 0, 23)
    r = rng.random()
    if r < 0.6:
        dom = "*"
    elif r < 0.7:
        dom = rng.choice(["L", "L-2", "LW", "15W", "1W", "31W"])
    else:
        dom = _field(rng, 1, 31)
    month = "*" if rng.random() < 0.7 else _field(rng, 1, 12)
    r = rng.random()
    if r < 0.6:
        dow = "*"
    elif r < 0.75:
        dow = rng.choice(["0L", "5L", "0#2", "1#1", "6#5", "SUN", "MON-FRI", "SAT,SUN"])
    else:
        dow = _field(rng, 0, 7)
    return f"{minute} {hour} {dom} {month} {dow}"
