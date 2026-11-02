"""One rollup cell: exact additive aggregates plus two mergeable sketches."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .ddsketch import DDSketch
from .events import Event
from .hll import HyperLogLog

HLL_P = 12
DD_ALPHA = 0.01


@dataclass
class Rollup:
    n: int = 0
    errors: int = 0
    lat_sum: float = 0.0
    lat_min: float = math.inf
    lat_max: float = -math.inf
    users: HyperLogLog = field(default_factory=lambda: HyperLogLog(HLL_P))
    latency: DDSketch = field(default_factory=lambda: DDSketch(DD_ALPHA))

    def add(self, e: Event) -> None:
        self.n += 1
        self.errors += e.is_error
        self.lat_sum += e.latency_ms
        self.lat_min = min(self.lat_min, e.latency_ms)
        self.lat_max = max(self.lat_max, e.latency_ms)
        self.users.add(e.user_id)
        self.latency.add(e.latency_ms)

    def merge(self, o: "Rollup") -> "Rollup":
        self.n += o.n
        self.errors += o.errors
        self.lat_sum += o.lat_sum
        self.lat_min = min(self.lat_min, o.lat_min)
        self.lat_max = max(self.lat_max, o.lat_max)
        self.users.merge(o.users)
        self.latency.merge(o.latency)
        return self

    def summary(self, quantiles=(0.5, 0.95, 0.99)) -> dict:
        if self.n == 0:
            return {"requests": 0, "errors": 0, "error_rate": None, "distinct_users": 0,
                    "latency_mean_ms": None, "latency_min_ms": None, "latency_max_ms": None,
                    **{f"latency_p{int(q * 100)}_ms": None for q in quantiles}}
        out = {
            "requests": self.n,
            "errors": self.errors,
            "error_rate": round(self.errors / self.n, 6),
            "distinct_users": round(self.users.estimate()),
            "latency_mean_ms": round(self.lat_sum / self.n, 3),
            "latency_min_ms": round(self.lat_min, 3),
            "latency_max_ms": round(self.lat_max, 3),
        }
        for q in quantiles:
            out[f"latency_p{int(q * 100)}_ms"] = round(self.latency.quantile(q), 3)
        return out
