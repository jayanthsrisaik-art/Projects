"""Immutable sorted string table.

File layout (all integers big-endian):
  records:  [klen u32][vlen i32 (-1 = tombstone)][key][value] ...
  index:    [klen u32][key][offset u64] ... (every INDEX_EVERY-th record)
  bloom:    raw bit array
  footer:   [n_records u64][index_off u64][bloom_off u64][bloom_m u64][bloom_k u32][crc32 u32 of everything before] [MAGIC 8]
"""
import bisect
import os
import struct
import zlib
from typing import Iterable, Iterator

from .bloom import BloomFilter

MAGIC = b"LSMKV001"
FOOTER = struct.Struct(">QQQQII")
INDEX_EVERY = 16
TOMBSTONE = None


class CorruptSSTable(Exception):
    pass


def write_sstable(path: str, items: Iterable[tuple[bytes, bytes | None]], n_hint: int) -> int:
    """Write sorted (key, value|None) items. Returns record count."""
    bloom = BloomFilter(n_hint)
    index: list[tuple[bytes, int]] = []
    buf = bytearray()
    n = 0
    for key, val in items:
        if n % INDEX_EVERY == 0:
            index.append((key, len(buf)))
        bloom.add(key)
        buf += struct.pack(">Ii", len(key), -1 if val is None else len(val)) + key + (val or b"")
        n += 1
    index_off = len(buf)
    for key, off in index:
        buf += struct.pack(">I", len(key)) + key + struct.pack(">Q", off)
    bloom_off = len(buf)
    buf += bytes(bloom.bits)
    footer_body = FOOTER.pack(n, index_off, bloom_off, bloom.m, bloom.k, 0)[:-4]
    crc = zlib.crc32(bytes(buf) + footer_body)
    buf += footer_body + struct.pack(">I", crc) + MAGIC
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(buf)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return n


class SSTable:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        tail = FOOTER.size + len(MAGIC)
        if len(self.data) < tail or self.data[-len(MAGIC):] != MAGIC:
            raise CorruptSSTable(f"{path}: bad magic")
        body_end = len(self.data) - len(MAGIC) - 4
        (stored,) = struct.unpack(">I", self.data[body_end:body_end + 4])
        if zlib.crc32(self.data[:body_end]) != stored:
            raise CorruptSSTable(f"{path}: checksum mismatch")
        self.n, self.index_off, bloom_off, m, k, _ = FOOTER.unpack(self.data[len(self.data) - tail:len(self.data) - len(MAGIC)])
        self.bloom = BloomFilter(1, bits=self.data[bloom_off:body_end - (FOOTER.size - 4)], k=k, m=m)
        self.index_keys: list[bytes] = []
        self.index_offs: list[int] = []
        pos = self.index_off
        while pos < bloom_off:
            (kl,) = struct.unpack_from(">I", self.data, pos)
            self.index_keys.append(self.data[pos + 4:pos + 4 + kl])
            (off,) = struct.unpack_from(">Q", self.data, pos + 4 + kl)
            self.index_offs.append(off)
            pos += 4 + kl + 8
        self.min_key = self.index_keys[0] if self.index_keys else None

    def _read(self, pos: int):
        kl, vl = struct.unpack_from(">Ii", self.data, pos)
        pos += 8
        key = self.data[pos:pos + kl]
        pos += kl
        if vl < 0:
            return key, None, pos
        return key, self.data[pos:pos + vl], pos + vl

    def get(self, key: bytes):
        """Return (found, value). value None means tombstone."""
        if key not in self.bloom or not self.index_keys:
            return False, None
        i = bisect.bisect_right(self.index_keys, key) - 1
        if i < 0:
            return False, None
        pos = self.index_offs[i]
        while pos < self.index_off:
            k, v, pos = self._read(pos)
            if k == key:
                return True, v
            if k > key:
                break
        return False, None

    def items(self) -> Iterator[tuple[bytes, bytes | None]]:
        pos = 0
        while pos < self.index_off:
            k, v, pos = self._read(pos)
            yield k, v
