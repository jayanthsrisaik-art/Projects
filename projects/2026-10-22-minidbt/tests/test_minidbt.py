import shutil
import sqlite3
from pathlib import Path

import pytest

from minidbt.cli import main
from minidbt.project import ProjectError, build_order, load_project, parse_model, render, select
from minidbt.runner import run, run_tests

EXAMPLE = Path(__file__).resolve().parent.parent / "example"


@pytest.fixture
def proj():
    return load_project(EXAMPLE)


def make(tmp_path, models, seeds=None, tests=""):
    (tmp_path / "models").mkdir()
    (tmp_path / "seeds").mkdir()
    for k, v in models.items():
        (tmp_path / "models" / f"{k}.sql").write_text(v)
    for k, v in (seeds or {}).items():
        (tmp_path / "seeds" / f"{k}.csv").write_text(v)
    (tmp_path / "tests.toml").write_text(tests)
    return load_project(tmp_path)


def test_order_respects_deps(proj):
    order = build_order(proj)
    assert order.index("stg_orders") < order.index("daily_revenue")
    assert order.index("stg_customers") < order.index("customer_ltv")
    assert order == build_order(proj)  # deterministic


def test_cycle_detected(tmp_path):
    p = make(tmp_path, {"a": "SELECT * FROM {{ ref('b') }}", "b": "SELECT * FROM {{ ref('a') }}"})
    with pytest.raises(ProjectError, match="cycle"):
        build_order(p)


def test_unknown_ref_and_bad_config(tmp_path):
    with pytest.raises(ProjectError, match="unknown relation"):
        make(tmp_path, {"a": "SELECT * FROM {{ ref('zzz') }}"})
    with pytest.raises(ProjectError, match="unique_key"):
        parse_model("m", "{{ config(materialized='incremental') }} SELECT 1")
    with pytest.raises(ProjectError, match="materialization"):
        parse_model("m", "{{ config(materialized='magic') }} SELECT 1")


def test_render_incremental_block(proj):
    m = proj.models["daily_revenue"]
    assert "MAX(order_date)" not in render(m, False)
    assert "FROM daily_revenue)" in render(m, True)


def test_selectors(proj):
    assert select(proj, "daily_revenue") == {"daily_revenue"}
    assert select(proj, "+daily_revenue") == {"daily_revenue", "stg_orders"}
    assert select(proj, "stg_orders+") == {"stg_orders", "daily_revenue", "customer_ltv"}
    with pytest.raises(ProjectError):
        select(proj, "nope")


def test_full_run_expected_output(proj):
    conn = sqlite3.connect(":memory:")
    res = run(conn, proj)
    assert all(r.status == "ok" for r in res)
    rows = conn.execute("SELECT * FROM daily_revenue ORDER BY order_date").fetchall()
    assert rows == [("2026-01-01", 65.5, 2), ("2026-01-02", 60.0, 2), ("2026-01-03", 80.0, 1)]
    ltv = dict(conn.execute("SELECT name, lifetime_paid FROM customer_ltv").fetchall())
    assert ltv == {"Asha": 40.0, "Ben": 25.5, "Chen": 60.0, "Dara": 80.0}
    assert all(r.status == "pass" for r in run_tests(conn, proj))


def test_incremental_merges_without_duplicates(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "p")
    p = tmp_path / "p"
    conn = sqlite3.connect(":memory:")
    run(conn, load_project(p))
    # new day arrives and the latest existing day gets a late order
    with open(p / "seeds" / "raw_orders.csv", "a") as fh:
        fh.write("106,3,paid,5.0,2026-01-03\n107,1,paid,7.0,2026-01-04\n")
    # re-run only the incremental model chain without wiping the target
    proj = load_project(p)
    res = run(conn, proj, "daily_revenue")
    assert [r.status for r in res if r.name == "daily_revenue"] == ["ok"]
    assert "merged" in next(r.detail for r in res if r.name == "daily_revenue")
    rows = conn.execute("SELECT * FROM daily_revenue ORDER BY order_date").fetchall()
    assert rows == [("2026-01-01", 65.5, 2), ("2026-01-02", 60.0, 2), ("2026-01-03", 85.0, 2), ("2026-01-04", 7.0, 1)]
    # full refresh yields identical result
    run(conn, proj, full_refresh=True)
    assert conn.execute("SELECT * FROM daily_revenue ORDER BY order_date").fetchall() == rows


def test_failure_skips_downstream(tmp_path):
    p = make(tmp_path, {"a": "SELECT nope FROM missing", "b": "SELECT * FROM {{ ref('a') }}", "c": "SELECT 1 AS x"})
    res = {r.name: r.status for r in run(sqlite3.connect(":memory:"), p)}
    assert res == {"a": "error", "b": "skipped", "c": "ok"}


def test_data_tests_detect_violations(tmp_path):
    tests = """
[[test]]
type = "unique"
model = "t"
column = "id"
[[test]]
type = "not_null"
model = "t"
column = "v"
[[test]]
type = "accepted_values"
model = "t"
column = "v"
values = ["x"]
[[test]]
type = "sql"
model = "t"
query = "SELECT * FROM t WHERE id > 100"
"""
    p = make(tmp_path, {"t": "SELECT * FROM {{ ref('s') }}"}, {"s": "id,v\n1,x\n1,y\n2,\n"}, tests)
    conn = sqlite3.connect(":memory:")
    run(conn, p)
    r = {x.name: x for x in run_tests(conn, p)}
    assert r["unique:t.id"].detail == "1 failing rows"
    assert r["not_null:t.v"].status == "fail"
    assert r["accepted_values:t.v"].detail == "1 failing rows"  # 'y'; NULL ignored
    assert r["sql:t"].status == "pass"


def test_unsafe_identifier_rejected(tmp_path):
    p = make(tmp_path, {"t": "SELECT 1 AS id"}, tests='[[test]]\ntype="unique"\nmodel="t"\ncolumn="id; DROP TABLE t"\n')
    conn = sqlite3.connect(":memory:")
    run(conn, p)
    assert run_tests(conn, p)[0].status == "fail"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_cli_build_and_lineage(capsys):
    assert main(["build", "--project", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "FAIL" not in out
    assert main(["lineage", "--project", str(EXAMPLE)]) == 0
    assert "daily_revenue [incremental] <- stg_orders" in capsys.readouterr().out
    assert main(["lineage", "--project", "/nonexistent"]) in (0, 2)
