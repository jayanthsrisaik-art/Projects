import random
from pathlib import Path
import subprocess, sys

import pytest

from scd2 import SnapshotError, Warehouse
from scd2.cli import main

ROOT = Path(__file__).resolve().parents[1]
SNAPS = ROOT / "samples" / "snapshots"
H = ["id", "name", "tier"]


def reference_versions(snaps: dict[str, dict[str, tuple]]):
    """Naive Python SCD2 used as an oracle: replay snapshots in date order."""
    dates = sorted(snaps)
    open_v, out = {}, []
    for i, d in enumerate(dates):
        cur = snaps[d]
        for k in list(open_v):
            if k not in cur or cur[k] != open_v[k][0]:
                out.append((k, *open_v[k][0], open_v[k][1], d))
                del open_v[k]
        for k, v in cur.items():
            if k not in open_v:
                open_v[k] = (v, d)
    for k, (v, d) in open_v.items():
        out.append((k, *v, d, None))
    return sorted(out, key=lambda r: (r[0], r[-2]))


def load(wh, d, recs, **kw):
    return wh.load_rows(H, [[k, *v] for k, v in recs.items()], d, "id", **kw)


def dim_rows(wh):
    return [tuple(r) for r in wh.con.execute(
        "SELECT id, name, tier, valid_from, valid_to FROM dim_scd2 ORDER BY id, valid_from")]


def test_change_delete_and_reappear():
    wh = Warehouse()
    load(wh, "2026-01-01", {"1": ("a", "x"), "2": ("b", "x")})
    load(wh, "2026-01-02", {"1": ("a", "y")})            # 1 changes, 2 deleted
    load(wh, "2026-01-03", {"1": ("a", "y"), "2": ("b", "x")})  # 2 reappears
    assert dim_rows(wh) == [
        ("1", "a", "x", "2026-01-01", "2026-01-02"),
        ("1", "a", "y", "2026-01-02", None),
        ("2", "b", "x", "2026-01-01", "2026-01-02"),
        ("2", "b", "x", "2026-01-03", None),
    ]
    assert wh.check() == []


def test_reload_same_snapshot_is_noop():
    wh = Warehouse()
    assert load(wh, "2026-01-01", {"1": ("a", "x")}) == "loaded"
    before = dim_rows(wh)
    assert load(wh, "2026-01-01", {"1": ("a", "x")}) == "unchanged"
    assert dim_rows(wh) == before


def test_conflicting_reload_needs_replace():
    wh = Warehouse()
    load(wh, "2026-01-01", {"1": ("a", "x")})
    with pytest.raises(SnapshotError, match="different content"):
        load(wh, "2026-01-01", {"1": ("a", "z")})
    assert load(wh, "2026-01-01", {"1": ("a", "z")}, replace=True) == "replaced"
    assert dim_rows(wh)[0][2] == "z"


def test_late_arriving_snapshot_matches_in_order_load():
    days = {
        "2026-01-01": {"1": ("a", "x")},
        "2026-01-02": {"1": ("a", "y")},
        "2026-01-03": {"1": ("a", "y"), "2": ("b", "x")},
    }
    a, b = Warehouse(), Warehouse()
    for d in sorted(days):
        load(a, d, days[d])
    for d in ["2026-01-03", "2026-01-01", "2026-01-02"]:
        load(b, d, days[d])
    assert dim_rows(a) == dim_rows(b)


@pytest.mark.parametrize("seed", range(25))
def test_random_histories_match_reference_and_pass_checks(seed):
    rng = random.Random(seed)
    n_days = rng.randint(2, 8)
    dates = [f"2026-02-{d:02d}" for d in range(1, n_days + 1)]
    snaps = {}
    for d in dates:
        snaps[d] = {str(k): (rng.choice("ab"), rng.choice("xyz"))
                    for k in range(6) if rng.random() < 0.7}
        if not snaps[d]:
            snaps[d] = {"0": ("a", "x")}
    order = dates[:]
    rng.shuffle(order)  # arrival order must not matter
    wh = Warehouse()
    for d in order:
        load(wh, d, snaps[d])
    assert wh.check() == []
    got = [(r[0], r[1], r[2], r[3], r[4]) for r in dim_rows(wh)]
    assert got == reference_versions(snaps)


def test_check_detects_corruption():
    wh = Warehouse()
    load(wh, "2026-01-01", {"1": ("a", "x")})
    load(wh, "2026-01-02", {"1": ("a", "y")})
    wh.con.execute("UPDATE dim_scd2 SET valid_to=NULL WHERE valid_from='2026-01-01'")
    problems = wh.check()
    assert any("overlapping" in p or "more than one open" in p for p in problems)
    assert any("reconstruction" in p for p in problems)


@pytest.mark.parametrize("header,rows,as_of,match", [
    (["id", "name"], [["1", "a"], ["1", "b"]], "2026-01-01", "duplicate key"),
    (["id", "name"], [["", "a"]], "2026-01-01", "empty key"),
    (["id", "name"], [["1"]], "2026-01-01", "expected 2 fields"),
    (["id", "name"], [["1", "a"]], "01/01/2026", "YYYY-MM-DD"),
    (["id", "valid_from"], [["1", "a"]], "2026-01-01", "reserved"),
    (["name"], [["a"]], "2026-01-01", "not in header"),
])
def test_input_validation(header, rows, as_of, match):
    with pytest.raises(SnapshotError, match=match):
        Warehouse().load_rows(header, rows, as_of, "id")


def test_schema_mismatch_rejected():
    wh = Warehouse()
    wh.load_rows(["id", "name"], [["1", "a"]], "2026-01-01", "id")
    with pytest.raises(SnapshotError, match="schema mismatch"):
        wh.load_rows(["id", "other"], [["1", "a"]], "2026-01-02", "id")


def test_sql_injection_in_column_name_rejected():
    with pytest.raises(SnapshotError):
        Warehouse().load_rows(["id", 'x"; DROP TABLE meta; --'], [["1", "a"]], "2026-01-01", "id")


def test_cli_demo_matches_expected_output():
    out = subprocess.run(["sh", str(ROOT / "samples" / "run_demo.sh")], capture_output=True,
                         text=True, check=True, cwd=ROOT, env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHONPATH": str(ROOT)}).stdout
    assert out == (ROOT / "samples" / "expected" / "demo_output.txt").read_text()


def test_cli_exit_codes(tmp_path, capsys):
    db = str(tmp_path / "w.db")
    assert main(["--db", db, "load", str(SNAPS / "customers_2026-01-01.csv"),
                 "--as-of", "2026-01-01", "--key", "customer_id"]) == 0
    assert main(["--db", db, "check"]) == 0
    assert main(["--db", db, "load", str(SNAPS / "customers_2026-01-01.csv"),
                 "--as-of", "bad", "--key", "customer_id"]) == 2
    assert "error:" in capsys.readouterr().err
