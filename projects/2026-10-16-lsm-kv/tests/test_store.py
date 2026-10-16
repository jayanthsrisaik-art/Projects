import io
import os
import random
import struct

import pytest

from lsmkv import BloomFilter, LSMStore
from lsmkv.cli import run
from lsmkv.sstable import CorruptSSTable, SSTable
from lsmkv.wal import WAL


def test_put_get_delete(tmp_path):
    with LSMStore(str(tmp_path)) as db:
        db.put(b"a", b"1")
        db.put(b"a", b"2")
        assert db.get(b"a") == b"2"
        db.delete(b"a")
        assert db.get(b"a") is None
        assert db.get(b"missing") is None


def test_rejects_bad_keys(tmp_path):
    with LSMStore(str(tmp_path)) as db:
        with pytest.raises(ValueError):
            db.put(b"", b"x")
        with pytest.raises(ValueError):
            db.put("str", b"x")


def test_tombstone_shadows_older_sstable(tmp_path):
    db = LSMStore(str(tmp_path), compact_at=99)
    db.put(b"k", b"v")
    db.flush()
    db.delete(b"k")
    db.flush()
    assert db.get(b"k") is None
    assert list(db.scan()) == []
    db.compact()
    assert len(db.tables) == 1 and db.tables[0].n == 0
    db.close()


def test_crash_recovery_from_wal(tmp_path):
    db = LSMStore(str(tmp_path))
    db.put(b"x", b"1")
    db.delete(b"y")
    db.wal.close()  # simulate crash: no flush
    db2 = LSMStore(str(tmp_path))
    assert db2.get(b"x") == b"1"
    db2.close()


def test_torn_wal_tail_is_ignored(tmp_path):
    db = LSMStore(str(tmp_path))
    db.put(b"good", b"1")
    db.put(b"torn", b"2222")
    db.wal.close()
    p = os.path.join(str(tmp_path), "wal.log")
    with open(p, "r+b") as f:
        f.truncate(os.path.getsize(p) - 3)
    assert dict(WAL.replay(p)) == {b"good": b"1"}


def test_persistence_across_reopen(tmp_path):
    with LSMStore(str(tmp_path), memtable_limit=64) as db:
        for i in range(100):
            db.put(f"k{i:03d}".encode(), b"v" * 10)
    with LSMStore(str(tmp_path)) as db:
        assert db.get(b"k042") == b"v" * 10
        assert len(list(db.scan())) == 100


def test_compaction_reduces_tables_and_keeps_data(tmp_path):
    db = LSMStore(str(tmp_path), memtable_limit=50, compact_at=3)
    for i in range(60):
        db.put(f"k{i % 20:02d}".encode(), f"v{i}".encode())
    db.flush()
    assert len(db.tables) < 3
    assert len(list(db.scan())) == 20
    assert db.get(b"k05") == b"v45"
    db.close()


def test_scan_range(tmp_path):
    with LSMStore(str(tmp_path), memtable_limit=40) as db:
        for c in "abcdefg":
            db.put(c.encode(), c.encode())
        assert [k for k, _ in db.scan(b"c", b"f")] == [b"c", b"d", b"e"]


def test_randomized_against_dict_model(tmp_path):
    rng = random.Random(7)
    model, db = {}, LSMStore(str(tmp_path), memtable_limit=200, compact_at=4)
    for step in range(3000):
        k = f"key{rng.randrange(150):03d}".encode()
        r = rng.random()
        if r < 0.6:
            v = str(step).encode()
            db.put(k, v)
            model[k] = v
        elif r < 0.8:
            db.delete(k)
            model.pop(k, None)
        elif r < 0.82:
            db.close()
            db = LSMStore(str(tmp_path), memtable_limit=200, compact_at=4)
        else:
            assert db.get(k) == model.get(k)
    assert list(db.scan()) == sorted(model.items())
    db.close()


def test_sstable_checksum_detects_corruption(tmp_path):
    with LSMStore(str(tmp_path)) as db:
        db.put(b"a", b"b")
    sst = [f for f in os.listdir(tmp_path) if f.endswith(".sst")][0]
    p = os.path.join(str(tmp_path), sst)
    data = bytearray(open(p, "rb").read())
    data[10] ^= 0xFF
    open(p, "wb").write(data)
    with pytest.raises(CorruptSSTable):
        SSTable(p)


def test_bloom_no_false_negatives_and_low_fp():
    bf = BloomFilter(1000, 0.01)
    for i in range(1000):
        bf.add(f"in{i}".encode())
    assert all(f"in{i}".encode() in bf for i in range(1000))
    fp = sum(f"out{i}".encode() in bf for i in range(5000)) / 5000
    assert fp < 0.03


def test_cli_sample(tmp_path):
    here = os.path.dirname(os.path.dirname(__file__))
    out = io.StringIO()
    with LSMStore(str(tmp_path)) as db:
        run(db, open(os.path.join(here, "samples", "script.txt")), out)
    assert out.getvalue() == open(os.path.join(here, "samples", "expected_output.txt")).read()
