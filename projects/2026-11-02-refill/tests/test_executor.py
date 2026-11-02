import random

import pytest

from refill import executor as ex
from refill.planner import plan, strategy_same_key
from refill.spec import parse_change
from refill.synth import SCENARIOS, generate
from helpers import example_pipeline

# A short range keeps property tests fast while still crossing a month boundary.
P = example_pipeline("2026-09-20", "2026-10-12")


def _build(data):
    conn = ex.connect()
    ex.create_schema(conn, P)
    ex.load_sources(conn, P, data)
    ex.full_build(conn, P)
    return conn


def _copy(conn):
    dst = ex.connect()
    conn.backup(dst)
    return dst


@pytest.fixture(scope="module")
def base():
    data = generate(P.cal, seed=3, customers=60, orders_per_day=20)
    return data, _build(data)


def _mutate_random(data, rng):
    """Randomly edit a few order/refund partitions; return (new data, changed roots)."""
    days = P.cal.partitions("day")
    new = {k: list(v) for k, v in data.items()}
    roots = set()
    for _ in range(rng.randint(1, 3)):
        d = rng.choice(days)
        table = rng.choice(["raw_orders", "raw_refunds"])
        rows = new[table]
        oid = 10_000_000 + rng.randint(0, 10**6)
        rows.append((d, oid, rng.randint(1, 60), rng.randint(100, 9000)))
        roots.add((table, d))
    return new, roots


def test_full_build_is_idempotent(base):
    data, conn = base
    snap = ex.snapshot(conn, P)
    again = _copy(conn)
    ex.full_build(again, P)
    assert ex.snapshot(again, P) == snap


@pytest.mark.parametrize("cutoff", [False, True])
def test_planned_backfill_equals_full_rebuild(base, cutoff):
    data, conn = base
    rng = random.Random(42 + cutoff)
    for _ in range(12):
        new, roots = _mutate_random(data, rng)
        oracle = ex.snapshot(_build(new), P)
        work = _copy(conn)
        ex.load_sources(work, P, new)
        stats = ex.run_tasks(work, P, plan(P, roots).tasks, roots, cutoff=cutoff)
        assert ex.snapshot(work, P) == oracle
        assert stats.executed + stats.skipped == stats.planned


def test_same_key_baseline_produces_wrong_results(base):
    data, conn = base
    new, _ = _mutate_random(data, random.Random(1))
    new["raw_orders"].append(("2026-09-25", 99_999_999, 5, 12_345))
    roots = {("raw_orders", "2026-09-25")}
    oracle = ex.snapshot(_build(new), P)
    work = _copy(conn)
    ex.load_sources(work, P, new)
    ex.run_tasks(work, P, strategy_same_key(P, roots), roots)
    assert ex.snapshot(work, P) != oracle


def test_cutoff_stops_on_identical_reland(base):
    data, conn = base
    roots = {("raw_orders", "2026-09-28")}
    work = _copy(conn)
    ex.load_sources(work, P, data)  # identical content re-landed
    stats = ex.run_tasks(work, P, plan(P, roots).tasks, roots, cutoff=True)
    assert stats.executed == 1 and stats.changed == 0
    assert stats.skipped == stats.planned - 1


def test_derived_logic_root_is_forced_even_with_cutoff(base):
    _, conn = base
    roots = {("monthly_region_sales", "2026-10")}
    work = _copy(conn)
    stats = ex.run_tasks(work, P, plan(P, roots).tasks, roots, cutoff=True)
    assert stats.executed >= 1


def test_run_task_overwrites_partition_not_appends(base):
    _, conn = base
    work = _copy(conn)
    node = ("region_sales_7d", "2026-10-01")
    before = ex.partition_digest(work, *node)
    n_rows = work.execute("SELECT COUNT(*) FROM region_sales_7d WHERE part='2026-10-01'").fetchone()[0]
    ex.run_task(work, P, node)
    ex.run_task(work, P, node)
    assert ex.partition_digest(work, *node) == before
    assert work.execute("SELECT COUNT(*) FROM region_sales_7d WHERE part='2026-10-01'").fetchone()[0] == n_rows


def test_scenarios_declare_their_changes_correctly():
    """Each demo scenario's declared roots must cover every source partition it edits."""
    full = example_pipeline()
    for sc in SCENARIOS:
        new = sc.mutate(generate(full.cal, seed=7))
        old = generate(full.cal, seed=7)
        roots = {n for c in sc.changes for n in parse_change(full, c)}
        for table in old:
            for part in {r[0] for r in old[table]} | {r[0] for r in new[table]}:
                a = sorted(r for r in old[table] if r[0] == part)
                b = sorted(r for r in new[table] if r[0] == part)
                if a != b:
                    assert (table, part) in roots, (sc.name, table, part)
