"""Small Bloom filter using double hashing over blake2b."""
import hashlib
import math


class BloomFilter:
    def __init__(self, n_items: int, fp_rate: float = 0.01, *, bits: bytes | None = None, k: int | None = None, m: int | None = None):
        n_items = max(n_items, 1)
        self.m = m or max(8, int(-n_items * math.log(fp_rate) / (math.log(2) ** 2)))
        self.k = k or max(1, round(self.m / n_items * math.log(2)))
        self.bits = bytearray(bits) if bits is not None else bytearray((self.m + 7) // 8)

    def _positions(self, key: bytes):
        d = hashlib.blake2b(key, digest_size=16).digest()
        h1 = int.from_bytes(d[:8], "little")
        h2 = int.from_bytes(d[8:], "little") | 1
        return ((h1 + i * h2) % self.m for i in range(self.k))

    def add(self, key: bytes) -> None:
        for p in self._positions(key):
            self.bits[p >> 3] |= 1 << (p & 7)

    def __contains__(self, key: bytes) -> bool:
        return all(self.bits[p >> 3] & (1 << (p & 7)) for p in self._positions(key))
