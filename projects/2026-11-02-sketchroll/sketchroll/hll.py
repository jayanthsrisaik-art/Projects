"""HyperLogLog distinct counter with Ertl's improved estimator.

Registers merge by element-wise max, so the sketch of a union is exactly the
merge of the sketches of its parts. That is the property that lets hourly
rollups be re-aggregated to any range without re-reading raw events.

The estimator is the "improved raw estimator" from O. Ertl, *New cardinality
estimation algorithms for HyperLogLog sketches* (2017). Unlike the original
HLL estimator it needs no empirical bias tables and no switch to linear
counting, and it is accurate from tiny to very large cardinalities.
"""
from __future__ import annotations

import hashlib
import math
import zlib

_MAGIC = b"HL1"


def hash64(value: str) -> int:
    """Stable 64-bit hash (independent of PYTHONHASHSEED)."""
    return int.from_bytes(hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(), "big")


def _sigma(x: float) -> float:
    if x == 1.0:
        return math.inf
    y = 1.0
    z = x
    while True:
        x *= x
        z_old = z
        z += x * y
        y += y
        if z == z_old:
            return z


def _tau(x: float) -> float:
    if x == 0.0 or x == 1.0:
        return 0.0
    y = 1.0
    z = 1.0 - x
    while True:
        x = math.sqrt(x)
        z_old = z
        y *= 0.5
        z -= (1.0 - x) ** 2 * y
        if z == z_old:
            return z / 3.0


class HyperLogLog:
    """HLL with 2**p one-byte registers over a 64-bit hash."""

    __slots__ = ("p", "m", "q", "registers")

    def __init__(self, p: int = 12) -> None:
        if not 4 <= p <= 16:
            raise ValueError("p must be in [4, 16]")
        self.p = p
        self.m = 1 << p
        self.q = 64 - p
        self.registers = bytearray(self.m)

    # -- updates -----------------------------------------------------------
    def add(self, value: str) -> None:
        self.add_hash(hash64(value))

    def add_hash(self, h: int) -> None:
        idx = h >> self.q
        w = h & ((1 << self.q) - 1)
        rho = self.q - w.bit_length() + 1  # position of leftmost 1, q+1 if w == 0
        if rho > self.registers[idx]:
            self.registers[idx] = rho

    def merge(self, other: "HyperLogLog") -> "HyperLogLog":
        """In-place union. Both sketches must share precision."""
        if other.p != self.p:
            raise ValueError(f"cannot merge p={self.p} with p={other.p}")
        a, b = self.registers, other.registers
        for i in range(self.m):
            if b[i] > a[i]:
                a[i] = b[i]
        return self

    def copy(self) -> "HyperLogLog":
        c = HyperLogLog(self.p)
        c.registers[:] = self.registers
        return c

    # -- estimate ----------------------------------------------------------
    def estimate(self) -> float:
        m, q = self.m, self.q
        hist = [0] * (q + 2)
        for r in self.registers:
            hist[r] += 1
        if hist[0] == m:
            return 0.0
        z = m * _tau(1.0 - hist[q + 1] / m)
        for k in range(q, 0, -1):
            z = 0.5 * (z + hist[k])
        z += m * _sigma(hist[0] / m)
        return m * m / (2.0 * math.log(2.0)) / z

    @property
    def relative_std_error(self) -> float:
        return 1.04 / math.sqrt(self.m)

    # -- serialisation -----------------------------------------------------
    def to_bytes(self) -> bytes:
        return _MAGIC + bytes([self.p]) + zlib.compress(bytes(self.registers), 6)

    @classmethod
    def from_bytes(cls, blob: bytes) -> "HyperLogLog":
        if blob[:3] != _MAGIC:
            raise ValueError("not a sketchroll HLL blob")
        h = cls(blob[3])
        regs = zlib.decompress(blob[4:])
        if len(regs) != h.m:
            raise ValueError("corrupt HLL blob: register count mismatch")
        h.registers[:] = regs
        return h

    def __eq__(self, other: object) -> bool:
        return isinstance(other, HyperLogLog) and self.p == other.p and self.registers == other.registers
