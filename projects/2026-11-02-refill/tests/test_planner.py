import random

from refill.planner import (brute_force_affected, explain, forward_deps, plan,
                            strategy_from_earliest, strategy_same_key)
from refill.spec import parse_change
from helpers import example_pipeline

P = example_pipeline()


def _days(a, b):
    return [k for k in P.cal.partitions("day") if a <= k <= b]


def test_late_orders_plan_is_exactly_expected():
    pl = plan(P, parse_change(P, "raw_orders:2026-10-05..2026-10-06"))
    got = pl.by_table()
    assert got["daily_region_sales"] == ["2026-10-05", "2026-10-06"]
    assert got["region_sales_7d"] == _days("2026-10-05", "2026-10-12")   # window(7) fan-out
    assert got["dod_change"] == _days("2026-10-05", "2026-10-07")        # lag(1)
    assert got["cumulative_net"] == _days("2026-10-05", "2026-10-31")    # self-lag chain to end
    assert got["monthly_region_sales"] == ["2026-10"]                    # rollup
    assert got["daily_share_of_month"] == _days("2026-10-01", "2026-10-31")  # month_of fans back
    assert len(pl.tasks) == 72


def test_dimension_change_invalidates_everything():
    pl = plan(P, parse_change(P, "dim_customer:*"))
    assert len(pl.tasks) == len(P.all_nodes(derived_only=True))


def test_logic_change_on_derived_table_is_a_task_itself():
    pl = plan(P, parse_change(P, "monthly_region_sales:2026-09"))
    got = pl.by_table()
    assert got == {"monthly_region_sales": ["2026-09"],
                   "daily_share_of_month": _days("2026-09-01", "2026-09-30")}


def test_planner_matches_brute_force_oracle_on_random_roots():
    rng = random.Random(0)
    nodes = [(t, k) for t in P.order for k in P.cal.partitions(P.grain(t))]
    for _ in range(60):
        roots = set(rng.sample(nodes, rng.randint(1, 4)))
        pl = plan(P, roots)
        assert set(pl.tasks) == brute_force_affected(P, roots)


def test_levels_respect_dependencies_and_explain_reaches_root():
    roots = parse_change(P, "raw_refunds:2026-09-14")
    pl = plan(P, roots)
    taskset = set(pl.tasks)
    pos = {n: i for i, n in enumerate(pl.tasks)}
    for n in pl.tasks:
        for d in forward_deps(P, n):
            if d in taskset:
                assert pl.level[d] < pl.level[n]
                assert pos[d] < pos[n]
        chain = explain(pl, n)
        assert chain[0] in roots and chain[-1] == n
    # cumulative_net 09-14..10-31 is a 48-long chain behind daily_region_sales
    assert pl.waves == 49


def test_from_earliest_baseline_misses_month_of_backward_fanout():
    roots = parse_change(P, "raw_orders:2026-10-05")
    truth = brute_force_affected(P, roots)
    missed = truth - set(strategy_from_earliest(P, roots))
    assert missed == {("daily_share_of_month", d) for d in _days("2026-10-01", "2026-10-04")}


def test_same_key_baseline_misses_window_and_chain():
    roots = parse_change(P, "raw_orders:2026-10-05")
    missed = brute_force_affected(P, roots) - set(strategy_same_key(P, roots))
    assert ("region_sales_7d", "2026-10-11") in missed
    assert ("cumulative_net", "2026-10-31") in missed
