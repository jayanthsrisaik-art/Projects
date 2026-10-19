import gzip
import json
from datetime import timedelta
from pathlib import Path

import pytest

from eventlake.cli import main
from eventlake.contract import Contract, validate
from eventlake.ingest import compact, ingest, read_partition

SAMPLES = Path(__file__).parent.parent / "samples"
CONTRACT = Contract.load(str(SAMPLES / "orders.contract.json"))


def rec(i, ts="2026-03-01T00:00:00Z", **kw):
    d = {"event_id": f"e{i}", "event_time": ts, "customer_id": i, "status": "paid", "amount": 1.0}
    d.update(kw)
    return d


def write(path, rows):
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
    return str(path)


@pytest.fixture
def env(tmp_path):
    return tmp_path, str(tmp_path / "lake"), str(tmp_path / "state.db")


def all_ids(lake):
    return {r["event_id"] for p in (Path(lake) / "orders").iterdir() for r in read_partition(lake, "orders", p.name)}


def test_validate_ok_and_each_violation():
    assert validate(CONTRACT, rec(1)) == []
    assert "amount: below min 0" in validate(CONTRACT, rec(1, amount=-1))
    assert "status: not in enum" in validate(CONTRACT, rec(1, status="x"))
    assert "customer_id: expected int" in validate(CONTRACT, rec(1, customer_id=True))
    assert "event_time: expected timestamp" in validate(CONTRACT, rec(1, ts="2026-03-01T00:00:00"))
    assert "customer_id: missing required" in validate(CONTRACT, {k: v for k, v in rec(1).items() if k != "customer_id"})
    assert validate(CONTRACT, [1]) == ["record: not a JSON object"]


def test_contract_rejects_bad_definitions():
    with pytest.raises(ValueError):
        Contract.from_dict({"name": "x", "key": "k", "time_field": "t",
                            "fields": [{"name": "k", "type": "string"}, {"name": "t", "type": "string"}]})
    with pytest.raises(ValueError):
        Contract.from_dict({"name": "x", "key": "k", "time_field": "t",
                            "fields": [{"name": "k", "type": "blob"}]})


def test_sample_run_matches_expected(env):
    _, lake, st = env
    rep = ingest(CONTRACT, [str(SAMPLES / "orders_day1.jsonl")], lake, st).to_dict()
    assert rep == json.loads((SAMPLES / "expected_day1_report.json").read_text())


def test_incremental_second_file_dedupes_and_quarantines_late(env):
    _, lake, st = env
    ingest(CONTRACT, [str(SAMPLES / "orders_day1.jsonl")], lake, st)
    d = ingest(CONTRACT, [str(SAMPLES / "orders_day1.jsonl"), str(SAMPLES / "orders_day2.jsonl")],
               lake, st).to_dict()
    assert (d["read"], d["accepted"], d["duplicates"], d["late"]) == (4, 2, 1, 1)
    assert all_ids(lake) == {"e1", "e2", "e5", "e7", "e9"}


def test_rerun_is_idempotent(env):
    tmp, lake, st = env
    src = write(tmp / "a.jsonl", [rec(1), rec(2, "2026-03-02T00:00:00Z")])
    ingest(CONTRACT, [src], lake, st)
    before = {p: p.read_bytes() for p in Path(lake).rglob("*.gz")}
    rep = ingest(CONTRACT, [src], lake, st)
    assert rep.read == 0 and rep.accepted == 0
    assert {p: p.read_bytes() for p in Path(lake).rglob("*.gz")} == before


def test_appended_lines_only_are_read(env):
    tmp, lake, st = env
    src = tmp / "a.jsonl"
    write(src, [rec(1)])
    ingest(CONTRACT, [str(src)], lake, st)
    write(src, [rec(1), rec(2)])
    rep = ingest(CONTRACT, [str(src)], lake, st)
    assert rep.read == 1 and rep.accepted == 1


def test_crash_before_commit_recovers_without_duplicates(env, monkeypatch):
    tmp, lake, st = env
    src = write(tmp / "a.jsonl", [rec(1), rec(2)])
    import eventlake.state as state_mod
    orig = state_mod.State.commit

    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(state_mod.State, "commit", boom)
    with pytest.raises(RuntimeError):
        ingest(CONTRACT, [src], lake, st)
    monkeypatch.setattr(state_mod.State, "commit", orig)
    rep = ingest(CONTRACT, [src], lake, st)
    assert rep.accepted == 2
    assert len(read_partition(lake, "orders", "dt=2026-03-01")) == 2
    assert len(list(Path(lake).rglob("part-*.gz"))) == 1


def test_duplicate_within_same_batch(env):
    tmp, lake, st = env
    rep = ingest(CONTRACT, [write(tmp / "a.jsonl", [rec(1), rec(1)])], lake, st)
    assert rep.accepted == 1 and rep.duplicates == 1


def test_lateness_window_is_configurable(env):
    tmp, lake, st = env
    src = write(tmp / "a.jsonl", [rec(1, "2026-03-05T00:00:00Z"), rec(2, "2026-03-03T00:00:00Z")])
    assert ingest(CONTRACT, [src], lake, st, timedelta(hours=24)).late == 1
    assert ingest(CONTRACT, [src], lake + "2", st + "2", timedelta(days=3)).late == 0


def test_timezone_normalised_to_utc_partition(env):
    tmp, lake, st = env
    ingest(CONTRACT, [write(tmp / "a.jsonl", [rec(1, "2026-03-01T23:30:00-05:00")])], lake, st)
    assert len(read_partition(lake, "orders", "dt=2026-03-02")) == 1


def test_quarantine_contents(env):
    tmp, lake, st = env
    ingest(CONTRACT, [write(tmp / "a.jsonl", [rec(1, amount=-3), "{oops"])], lake, st)
    qf = next(Path(lake, "_quarantine", "orders").glob("*.gz"))
    rows = [json.loads(l) for l in gzip.open(qf, "rt")]
    assert [r["reasons"] for r in rows] == [["amount: below min 0"], ["malformed_json"]]
    assert rows[0]["line"] == 1 and rows[1]["line"] == 2


def test_compact_preserves_records(env):
    tmp, lake, st = env
    src = tmp / "a.jsonl"
    write(src, [rec(1), rec(2)])
    ingest(CONTRACT, [str(src)], lake, st)
    write(src, [rec(1), rec(2), rec(3)])
    ingest(CONTRACT, [str(src)], lake, st)
    before = sorted(r["event_id"] for r in read_partition(lake, "orders", "dt=2026-03-01"))
    assert compact(lake, "orders") == {"dt=2026-03-01": 3}
    assert sorted(r["event_id"] for r in read_partition(lake, "orders", "dt=2026-03-01")) == before
    assert [p.name for p in Path(lake, "orders", "dt=2026-03-01").iterdir()] == ["compact.jsonl.gz"]
    write(src, [rec(1), rec(2), rec(3), rec(4), rec(1)])
    rep = ingest(CONTRACT, [str(src)], lake, st)
    assert rep.accepted == 1 and rep.duplicates == 1


def test_cli_end_to_end(env, capsys):
    _, lake, _ = env
    assert main(["ingest", "--contract", str(SAMPLES / "orders.contract.json"), "--lake", lake,
                 str(SAMPLES / "orders_day1.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] == 3
    main(["query", "--lake", lake, "--dataset", "orders", "--partition", "dt=2026-03-01"])
    assert [json.loads(l)["event_id"] for l in capsys.readouterr().out.splitlines()] == ["e1", "e2"]


def test_compact_twice_keeps_data(env):
    tmp, lake, st = env
    src = tmp / "a.jsonl"
    write(src, [rec(1)])
    ingest(CONTRACT, [str(src)], lake, st)
    compact(lake, "orders")
    write(src, [rec(1), rec(2)])
    ingest(CONTRACT, [str(src)], lake, st)
    assert compact(lake, "orders") == {"dt=2026-03-01": 2}
    assert all_ids(lake) == {"e1", "e2"}
