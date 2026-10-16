"""Write-ahead log: records [crc32 u32][klen u32][vlen i32][key][value]. Torn tail is discarded on replay."""
import os
import struct
import zlib

HDR = struct.Struct(">IIi")


class WAL:
    def __init__(self, path: str, sync: bool = False):
        self.path, self.sync = path, sync
        self.f = open(path, "ab")

    def append(self, key: bytes, val: bytes | None) -> None:
        body = struct.pack(">Ii", len(key), -1 if val is None else len(val)) + key + (val or b"")
        self.f.write(struct.pack(">I", zlib.crc32(body)) + body)
        self.f.flush()
        if self.sync:
            os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.close()

    def reset(self) -> None:
        self.f.close()
        open(self.path, "wb").close()
        self.f = open(self.path, "ab")

    @staticmethod
    def replay(path: str):
        if not os.path.exists(path):
            return
        with open(path, "rb") as f:
            data = f.read()
        pos = 0
        while pos + HDR.size <= len(data):
            crc, kl, vl = HDR.unpack_from(data, pos)
            end = pos + HDR.size + kl + max(vl, 0)
            if end > len(data):
                return  # torn write
            body = data[pos + 4:end]
            if zlib.crc32(body) != crc:
                return  # corrupt tail: stop
            key = data[pos + HDR.size:pos + HDR.size + kl]
            yield key, (None if vl < 0 else data[pos + HDR.size + kl:end])
            pos = end
