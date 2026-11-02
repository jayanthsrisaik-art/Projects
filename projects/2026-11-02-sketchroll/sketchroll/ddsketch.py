"""DDSketch: quantiles with a relative-error guarantee, exactly mergeable.

Values are bucketed by ``ceil(log_gamma(x))`` with ``gamma = (1+a)/(1-a)``.
Every value in bucket ``k`` lies in ``(gamma**(k-1), gamma**k]`` and the bucket
representative ``2*gamma**k/(gamma+1)`` is within relative error ``a`` of all of
them. A quantile answer is therefore within ``a`` of the true order statistic,
for any data and after any number of merges (bucket counts just add), as long
as the low-end collapse below never triggers for the quantile asked.

Reference: Masson, Rim, Lee, *DDSketch: A Fast and Fully-Mergeable Quantile
Sketch with Relative-Error Guarantees*, VLDB 2019.
"""
from __future__ import annotations

import json
import math
import zlib

_MAGIC = b"DD1"


class DDSketch:
    __slots__ = ("alpha", "gamma", "_log_gamma", "max_bins", "min_value", "bins", "zero_count", "count", "collapsed")

    def __init__(self, alpha: float = 0.01, max_bins: int = 2048, min_value: float = 1e-9) -> None:
        if not 0 < alpha < 1:
            raise ValueError("alpha must be in (0, 1)")
        self.alpha = alpha
        self.gamma = (1 + alpha) / (1 - alpha)
        self._log_gamma = math.log(self.gamma)
        self.max_bins = max_bins
        self.min_value = min_value
        self.bins: dict[int, int] = {}
        self.zero_count = 0
        self.count = 0
        self.collapsed = False  # True once low buckets were folded together

    def key(self, x: float) -> int:
        return math.ceil(math.log(x) / self._log_gamma)

    def value_of(self, key: int) -> float:
        return 2.0 * self.gamma ** key / (self.gamma + 1.0)

    def add(self, x: float, weight: int = 1) -> None:
        if x < 0 or math.isnan(x) or math.isinf(x):
            raise ValueError(f"DDSketch accepts finite non-negative values, got {x!r}")
        self.count += weight
        if x <= self.min_value:
            self.zero_count += weight
            return
        k = self.key(x)
        self.bins[k] = self.bins.get(k, 0) + weight
        if len(self.bins) > self.max_bins:
            self._collapse()

    def _collapse(self) -> None:
        """Fold the lowest buckets into one so at most ``max_bins`` remain.

        Keeps the guarantee for upper quantiles (latency p50/p95/p99), loses it
        only for ranks that land in the folded low end.
        """
        keys = sorted(self.bins)
        excess = len(keys) - self.max_bins
        target = keys[excess]
        moved = sum(self.bins.pop(k) for k in keys[:excess])
        self.bins[target] += moved
        self.collapsed = True

    def merge(self, other: "DDSketch") -> "DDSketch":
        if abs(other.gamma - self.gamma) > 1e-15:
            raise ValueError("cannot merge sketches with different alpha")
        for k, c in other.bins.items():
            self.bins[k] = self.bins.get(k, 0) + c
        self.zero_count += other.zero_count
        self.count += other.count
        self.collapsed = self.collapsed or other.collapsed
        if len(self.bins) > self.max_bins:
            self._collapse()
        return self

    def quantile(self, q: float) -> float:
        """Approximate the lower order statistic ``sorted(x)[floor(q*(n-1))]``."""
        if not 0 <= q <= 1:
            raise ValueError("q must be in [0, 1]")
        if self.count == 0:
            raise ValueError("quantile of an empty sketch")
        rank = int(q * (self.count - 1))
        cum = self.zero_count
        if cum > rank:
            return 0.0
        for k in sorted(self.bins):
            cum += self.bins[k]
            if cum > rank:
                return self.value_of(k)
        raise AssertionError("bin counts inconsistent with total count")  # pragma: no cover

    def copy(self) -> "DDSketch":
        c = DDSketch(self.alpha, self.max_bins, self.min_value)
        c.bins = dict(self.bins)
        c.zero_count, c.count, c.collapsed = self.zero_count, self.count, self.collapsed
        return c

    def to_bytes(self) -> bytes:
        keys = sorted(self.bins)
        payload = {
            "a": self.alpha, "mb": self.max_bins, "mv": self.min_value,
            "z": self.zero_count, "c": int(self.collapsed),
            "k": keys, "n": [self.bins[k] for k in keys],
        }
        return _MAGIC + zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)

    @classmethod
    def from_bytes(cls, blob: bytes) -> "DDSketch":
        if blob[:3] != _MAGIC:
            raise ValueError("not a sketchroll DDSketch blob")
        d = json.loads(zlib.decompress(blob[3:]))
        s = cls(d["a"], d["mb"], d["mv"])
        s.bins = dict(zip(d["k"], d["n"]))
        s.zero_count = d["z"]
        s.collapsed = bool(d["c"])
        s.count = s.zero_count + sum(d["n"])
        return s

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, DDSketch) and self.alpha == other.alpha and self.bins == other.bins
                and self.zero_count == other.zero_count and self.count == other.count)
