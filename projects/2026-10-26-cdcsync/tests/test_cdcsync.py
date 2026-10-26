import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcsync import Applier, NaiveApplier, RejectError, SimulatedCrash, parse_line, reconcile  # noqa: E402
from cdcsync.cli import main  # noqa: E402
from cdcsync.simulator import SourceSim, chaos, generate  # noqa: E402

SAMPLES = ROOT / "samples"


def change(table, op, key, after, lsn, tx=None):
    return json.dumps({"type": "change", "table": table, "op": op, "key": key,
                       "after": after, "lsn": lsn, "tx": tx})


def end(tx, n):
    return json.dumps({"type": "tx_end", "tx": tx, "event_count": n})


@pytest.fixture
def applier(tmp_path):
    a = Applier(tmp_path / "t.db")
    yield a
    a.close()


# ----------------------------------------------------------------- parsing
@pytest.mark.parametrize("line,reason", [
    ("not json", "invalid JSON"),
    (json.dumps({"type": "heartbeat"}), "unknown record type"),
    (change("bad name", "c", {"id": 1}, {"id": 1}, 1), "invalid table"),
    (change("_cdc_offsets", "c", {"id": 1}, {"id": 1}, 1), "reserved"),
    (change("t", "x", {"id": 1}, {"id": 1}, 1), "unknown op"),
    (change("t", "c", {"id": 1}, {"id": 1}, -1), "lsn"),
    (change("t", "c", {"id": 1}, {"id": 1}, True), "lsn"),
    (change("t", "c", {"id": 1}, {"id": 2}, 1), "key column"),
    (change("t", "d", {"id": 1}, {"id": 1}, 1), "after = null"),
    (change("t", "c", {"id": None}, {"id": None}, 1), "string or integer"),
    (change("t", "c", {"id": 1}, {"id": 1, "x": [1]}, 1), "scalar"),
    (change("t", "r", {"id": 1}, {"id": 1}, 1, tx="t1"), "snapshot"),
    (end("t1", 0), "event_count"),
])
def test_parse_rejects_malformed(line, reason):
    with pytest.raises(RejectError, match=reason):
        parse_line(line)


# -------------------------------------------------------------- sample I/O
def test_sample_matches_expected_outputs(applier):
    stats = applier.apply_file(SAMPLES / "events.jsonl")
    assert stats.as_dict() == json.loads((SAMPLES / "expected_stats.json").read_text())
    assert applier.dump() == json.loads((SAMPLES / "expected_state.json").read_text())


def test_cli_dump_matches_expected_state(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["apply", str(SAMPLES / "events.jsonl"), "--target", db]) == 0
    capsys.readouterr()
    assert main(["dump", "--target", db]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads((SAMPLES / "expected_state.json").read_text())


# -------------------------------------------------------------- semantics
def test_stale_update_is_skipped(applier):
    stats = applier.apply_lines([
        change("t", "c", {"id": 1}, {"id": 1, "v": "new"}, 20),
        change("t", "u", {"id": 1}, {"id": 1, "v": "old"}, 10),
    ], "s")
    assert applier.dump()["t"] == [{"id": 1, "v": "new"}]
    assert stats.stale_skipped == 1


def test_tombstone_blocks_resurrection_even_if_delete_arrives_first(applier):
    applier.apply_lines([
        change("t", "d", {"id": 1}, None, 30),          # delete for a row never seen
        change("t", "c", {"id": 1}, {"id": 1, "v": "a"}, 10),
        change("t", "u", {"id": 1}, {"id": 1, "v": "b"}, 20),
    ], "s")
    assert applier.dump()["t"] == []
    assert applier.status()["tables"]["t"]["tombstones"] == 1


def test_reinsert_after_delete_with_higher_lsn(applier):
    applier.apply_lines([
        change("t", "c", {"id": 1}, {"id": 1, "v": "a"}, 10),
        change("t", "d", {"id": 1}, None, 20),
        change("t", "c", {"id": 1}, {"id": 1, "v": "again"}, 30),
    ], "s")
    assert applier.dump()["t"] == [{"id": 1, "v": "again"}]


def test_transaction_applies_atomically_only_when_complete(applier):
    applier.apply_lines([
        change("acct", "u", {"id": 1}, {"id": 1, "bal": 50}, 10, "t1"),
        end("t1", 2),
    ], "s")
    assert applier.dump() == {}  # half a transfer must not be visible
    assert applier.status()["pending_tx"] == [{"tx": "t1", "expected": 2, "staged": 1}]
    stats = applier.apply_lines([
        change("acct", "u", {"id": 1}, {"id": 1, "bal": 50}, 10, "t1"),
        end("t1", 2),
        change("acct", "u", {"id": 2}, {"id": 2, "bal": 150}, 11, "t1"),
    ], "s2")
    assert stats.duplicates == 2 and stats.tx_committed == 1
    assert applier.dump()["acct"] == [{"bal": 50, "id": 1}, {"bal": 150, "id": 2}]
    assert applier.status()["pending_tx"] == []


def test_overfull_transaction_is_not_applied(applier):
    applier.apply_lines([
        change("t", "c", {"id": 1}, {"id": 1}, 10, "t1"),
        change("t", "c", {"id": 2}, {"id": 2}, 11, "t1"),
        end("t1", 1),
    ], "s")
    # tx_end announced 1 event but 2 distinct ones are staged: hold it.
    assert applier.dump() == {}
    assert applier.status()["pending_tx"][0]["staged"] == 2


def test_late_duplicate_of_committed_tx_is_dropped(applier):
    first = change("t", "c", {"id": 1}, {"id": 1, "v": "a"}, 10, "t1")
    applier.apply_lines([first, end("t1", 1)], "s")
    stats = applier.apply_lines([first], "s2")
    assert stats.duplicates == 1
    assert applier.status()["pending_tx"] == []  # nothing left dangling in staging


def test_additive_schema_evolution_rebuilds_view(applier):
    applier.apply_lines([
        change("c", "c", {"id": 1}, {"id": 1, "name": "a"}, 10),
        change("c", "c", {"id": 2}, {"id": 2, "name": "b", "tier": "gold"}, 11),
        # an old-schema image arriving later for a new key: tier stays NULL
        change("c", "c", {"id": 3}, {"id": 3, "name": "c"}, 9),
    ], "s")
    assert applier.dump()["c"] == [
        {"id": 1, "name": "a", "tier": None},
        {"id": 2, "name": "b", "tier": "gold"},
        {"id": 3, "name": "c", "tier": None},
    ]


def test_primary_key_change_is_rejected(applier):
    stats = applier.apply_lines([
        change("t", "c", {"id": 1}, {"id": 1}, 10),
        change("t", "c", {"other": 1}, {"other": 1, "id": 5}, 11),
    ], "s")
    assert stats.rejected == 1
    assert "key columns" in applier.status()["rejects"][0]["reason"]


def test_composite_key(applier):
    applier.apply_lines([
        change("li", "c", {"order_id": 1, "line": 1}, {"order_id": 1, "line": 1, "sku": "A"}, 10),
        change("li", "c", {"order_id": 1, "line": 2}, {"order_id": 1, "line": 2, "sku": "B"}, 11),
        change("li", "u", {"line": 1, "order_id": 1}, {"order_id": 1, "line": 1, "sku": "C"}, 12),
    ], "s")
    assert [r["sku"] for r in applier.dump()["li"]] == ["C", "B"]


def test_rejects_are_quarantined_not_fatal(applier):
    stats = applier.apply_lines(["{oops", "", change("t", "c", {"id": 1}, {"id": 1}, 1)], "s")
    assert stats.rejected == 1 and stats.applied == 1
    assert applier.status()["rejects"][0]["line"] == 1


# ------------------------------------------------- idempotence & recovery
def test_rerun_is_noop_because_offset_is_committed(tmp_path):
    a = Applier(tmp_path / "t.db")
    a.apply_file(SAMPLES / "events.jsonl")
    before = a.dump()
    again = a.apply_file(SAMPLES / "events.jsonl")
    assert again.lines_read == 0 and again.applied == 0
    assert a.dump() == before
    a.close()


def test_replaying_under_a_new_source_name_is_still_idempotent(applier):
    applier.apply_file(SAMPLES / "events.jsonl")
    before = applier.dump()
    stats = applier.apply_file(SAMPLES / "events.jsonl", source="replay")
    assert stats.applied == 0
    assert applier.dump() == before


def _simulated(tmp_path, seed=3, transactions=150, window=30):
    info = generate(tmp_path / "sim", seed=seed, transactions=transactions, window=window, dup_rate=0.1)
    return tmp_path / "sim", info


def test_crash_mid_batch_then_resume_converges(tmp_path):
    sim, _ = _simulated(tmp_path)
    lines = (sim / "events.jsonl").read_text().splitlines()

    def crash_at(n):
        def hook(line_no):
            if line_no == n:
                raise SimulatedCrash(n)
        return hook

    a = Applier(tmp_path / "crashy.db")
    for n in (37, 210, 211, 500):  # crash several times at different points
        with pytest.raises(SimulatedCrash):
            a.apply_lines(lines, "events", batch_size=50, crash_hook=crash_at(n))
        assert a.offset("events") == (n - 1) // 50 * 50
    a.apply_lines(lines, "events", batch_size=50)
    a.close()

    clean = Applier(tmp_path / "clean.db")
    clean.apply_lines(lines, "events", batch_size=50)
    crashed = Applier(tmp_path / "crashy.db")
    assert crashed.dump() == clean.dump()
    assert crashed.status()["pending_tx"] == clean.status()["pending_tx"] == []
    clean.close()
    crashed.close()
    assert reconcile(sim / "source.db", tmp_path / "crashy.db")["ok"]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_chaotic_stream_converges_to_source(tmp_path, seed):
    sim, info = _simulated(tmp_path, seed=seed, window=60)
    a = Applier(tmp_path / "t.db")
    stats = a.apply_file(sim / "events.jsonl", batch_size=97)
    a.close()
    assert stats.tx_committed == info["transactions"] and stats.pending_tx == 0
    assert stats.rejected == 3
    report = reconcile(sim / "source.db", tmp_path / "t.db")
    assert report["ok"], report
    assert report["tables"]["customers"]["source_rows"] == info["source_rows"]["customers"]


def test_any_permutation_converges(tmp_path):
    sim = SourceSim(seed=9, customers=5, orders=5)
    events = sim.snapshot() + sim.stream(40)
    sim.write_source_db(tmp_path / "src.db")
    lines = [json.dumps(e) for e in events]
    random.Random(0).shuffle(lines)  # unbounded reordering
    a = Applier(tmp_path / "t.db")
    a.apply_lines(lines, "s")
    a.close()
    assert reconcile(tmp_path / "src.db", tmp_path / "t.db")["ok"]


def test_naive_baseline_drifts_on_the_same_stream(tmp_path):
    sim, _ = _simulated(tmp_path, seed=3, window=60)
    n = NaiveApplier(tmp_path / "naive.db")
    n.apply_file(sim / "events.jsonl")
    n.close()
    assert not reconcile(sim / "source.db", tmp_path / "naive.db")["ok"]


# ----------------------------------------------------------- reconciliation
def test_reconcile_pinpoints_tampered_rows(tmp_path):
    sim, _ = _simulated(tmp_path, seed=4)
    a = Applier(tmp_path / "t.db")
    a.apply_file(sim / "events.jsonl")
    a.close()
    conn = sqlite3.connect(tmp_path / "t.db")
    victim = conn.execute("SELECT id FROM orders WHERE _cdc_deleted = 0 ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE orders SET status = 'tampered' WHERE id = ?", (victim,))
    conn.execute("UPDATE customers SET _cdc_deleted = 1 WHERE id = (SELECT MIN(id) FROM customers WHERE _cdc_deleted = 0)")
    conn.commit()
    conn.close()
    report = reconcile(sim / "source.db", tmp_path / "t.db")
    assert not report["ok"]
    assert report["tables"]["orders"]["value_mismatches"] == [{"key": [victim], "columns": ["status"]}]
    assert report["tables"]["orders"]["buckets_mismatched"] == 1
    assert report["tables"]["customers"]["missing_count"] == 1
    assert main(["reconcile", "--source", str(sim / "source.db"), "--target", str(tmp_path / "t.db")]) == 1


def test_chaos_is_bounded_and_deterministic():
    events = [{"type": "tx_end", "tx": f"t{i}", "event_count": 1} for i in range(200)]
    a = chaos(events, seed=1, window=10, dup_rate=0.0, malformed=0)
    assert a == chaos(events, seed=1, window=10, dup_rate=0.0, malformed=0)
    pos = {json.loads(line)["tx"]: i for i, line in enumerate(a)}
    assert max(abs(pos[f"t{i}"] - i) for i in range(200)) <= 10
