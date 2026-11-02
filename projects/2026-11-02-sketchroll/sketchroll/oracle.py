"""Ground truth and the naive-rollup baseline, both computed from raw events."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .events import Event, hour_floor

QUANTILES = (0.5, 0.95, 0.99)


def lower_quantile(sorted_values: list[float], q: float) -> float:
    return sorted_values[int(q * (len(sorted_values) - 1))]


def exact(events: list[Event], start: datetime, end: datetime, endpoints=None) -> dict:
    sel = [e for e in events if start <= e.ts < end and (not endpoints or e.endpoint in endpoints)]
    if not sel:
        return {"requests": 0}
    lat = sorted(e.latency_ms for e in sel)
    out = {
        "requests": len(sel),
        "errors": sum(e.is_error for e in sel),
        "distinct_users": len({e.user_id for e in sel}),
        "latency_sum_ms": sum(lat),
        "latency_min_ms": lat[0],
        "latency_max_ms": lat[-1],
    }
    for q in QUANTILES:
        out[f"latency_p{int(q * 100)}_ms"] = lower_quantile(lat, q)
    return out


class NaiveRollup:
    """The common anti-pattern: store per-hour scalars, re-aggregate with sum/avg.

    Each hourly cell is *exact* (computed from all raw events, late ones
    included), so every error this baseline shows comes from re-aggregation.
    """

    def __init__(self, events: list[Event]):
        groups = defaultdict(list)
        for e in events:
            groups[(e.endpoint, hour_floor(e.ts))].append(e)
        self.cells = {}
        for key, evs in groups.items():
            lat = sorted(e.latency_ms for e in evs)
            self.cells[key] = {
                "n": len(evs),
                "distinct_users": len({e.user_id for e in evs}),
                **{f"latency_p{int(q * 100)}_ms": lower_quantile(lat, q) for q in QUANTILES},
            }

    def query(self, start: datetime, end: datetime, endpoints=None) -> dict:
        cells = [c for (ep, h), c in self.cells.items()
                 if start <= h < end and (not endpoints or ep in endpoints)]
        n = sum(c["n"] for c in cells)
        if not n:
            return {"requests": 0}
        out = {"requests": n, "distinct_users": sum(c["distinct_users"] for c in cells)}
        for q in QUANTILES:
            k = f"latency_p{int(q * 100)}_ms"
            out[k] = sum(c[k] * c["n"] for c in cells) / n  # request-weighted average
        return out
