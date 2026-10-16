"""LSM-tree key-value store: WAL + memtable + SSTables with size-tiered full compaction."""
import heapq
import os
import re
from typing import Iterator

from .sstable import SSTable, write_sstable
from .wal import WAL

_SST = re.compile(r"^(\d{8})\.sst$")


def _tag(src, priority):
    """Tag records with a source priority (0 = newest) so merge ties resolve newest-first."""
    return ((k, priority, v) for k, v in src)


class LSMStore:
    def __init__(self, path: str, memtable_limit: int = 4096, compact_at: int = 4, sync: bool = False):
        self.dir, self.limit, self.compact_at = path, memtable_limit, compact_at
        os.makedirs(path, exist_ok=True)
        self.mem: dict[bytes, bytes | None] = {}
        self.mem_bytes = 0
        self.tables: list[SSTable] = []  # oldest -> newest
        self._seq = 0
        for name in sorted(os.listdir(path)):
            if name.endswith(".tmp"):
                os.remove(os.path.join(path, name))
            m = _SST.match(name)
            if m:
                self.tables.append(SSTable(os.path.join(path, name)))
                self._seq = max(self._seq, int(m.group(1)))
        walp = os.path.join(path, "wal.log")
        for k, v in WAL.replay(walp):
            self._mem_put(k, v)
        self.wal = WAL(walp, sync=sync)
        self.closed = False

    # -- public API
    def put(self, key: bytes, value: bytes) -> None:
        self._write(key, value)

    def delete(self, key: bytes) -> None:
        self._write(key, None)

    def get(self, key: bytes) -> bytes | None:
        if key in self.mem:
            return self.mem[key]
        for t in reversed(self.tables):
            found, v = t.get(key)
            if found:
                return v
        return None

    def scan(self, start: bytes = b"", end: bytes | None = None) -> Iterator[tuple[bytes, bytes]]:
        """Yield live (key, value) pairs in [start, end) sorted by key."""
        sources = [sorted(self.mem.items())] + [t.items() for t in reversed(self.tables)]
        # priority = source index (0 = newest)
        merged = heapq.merge(*[_tag(src, i) for i, src in enumerate(sources)])
        last = None
        for k, _, v in merged:
            if k == last:
                continue
            last = k
            if k < start:
                continue
            if end is not None and k >= end:
                break
            if v is not None:
                yield k, v

    def flush(self) -> None:
        if not self.mem:
            return
        self._seq += 1
        path = os.path.join(self.dir, f"{self._seq:08d}.sst")
        write_sstable(path, sorted(self.mem.items()), len(self.mem))
        self.tables.append(SSTable(path))
        self.mem, self.mem_bytes = {}, 0
        self.wal.reset()
        if len(self.tables) >= self.compact_at:
            self.compact()

    def compact(self) -> None:
        """Merge all tables into one; drops shadowed versions and tombstones."""
        if len(self.tables) < 2:
            return
        live = [(k, v) for k, v in self._merged_tables() if v is not None]
        old = self.tables
        self._seq += 1
        path = os.path.join(self.dir, f"{self._seq:08d}.sst")
        write_sstable(path, live, len(live))
        self.tables = [SSTable(path)]
        for t in old:
            os.remove(t.path)

    def close(self) -> None:
        if not self.closed:
            self.flush()
            self.wal.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- internals
    def _merged_tables(self):
        srcs = [_tag(t.items(), i) for i, t in enumerate(reversed(self.tables))]
        last = None
        for k, _, v in heapq.merge(*srcs):
            if k != last:
                last = k
                yield k, v

    def _mem_put(self, k, v):
        if k in self.mem:
            self.mem_bytes += len(v or b"") - len(self.mem[k] or b"")
        else:
            self.mem_bytes += len(k) + len(v or b"")
        self.mem[k] = v

    def _write(self, key: bytes, value: bytes | None) -> None:
        if not isinstance(key, bytes) or not key:
            raise ValueError("key must be non-empty bytes")
        self.wal.append(key, value)
        self._mem_put(key, value)
        if self.mem_bytes >= self.limit:
            self.flush()
