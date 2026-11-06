import random

import pytest

from tallyup.settle import MAX_EXACT, Transfer, apply, greedy_plan, max_zero_sum_groups, optimal_plan


def set_partitions(items):
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for part in set_partitions(rest):
        yield [[first]] + part
        for i in range(len(part)):
            yield part[:i] + [[first] + part[i]] + part[i + 1:]


def oracle_min_transfers(balances):
    """Independent oracle: enumerate every set partition of the non-zero members."""
    nz = [b for b in balances.values() if b]
    if not nz:
        return 0
    best = max(len(p) for p in set_partitions(nz) if all(sum(g) == 0 for g in p))
    return len(nz) - best


def random_balances(rng, n, scale=5000, structured=False):
    if structured:
        # Hidden zero-sum sub-groups (e.g. separate trips) - where greedy loses.
        bal, i = {}, 0
        while i < n:
            k = min(n - i, rng.randint(2, 3))
            vals = [rng.randint(1, scale) * rng.choice([-1, 1]) for _ in range(k - 1)]
            vals.append(-sum(vals))
            for v in vals:
                bal[i] = v
                i += 1
        return bal
    vals = [rng.randint(-scale, scale) for _ in range(n - 1)]
    vals.append(-sum(vals))
    return dict(enumerate(vals))


def assert_settles(bal, transfers):
    after = apply(bal, transfers)
    assert all(v == 0 for v in after.values()), after
    assert all(t.amount > 0 for t in transfers)


def test_known_case_where_greedy_loses():
    # {+5,-5} and {+3,+4,-7} settle in 1 + 2 = 3 transfers; greedy pairs +5/-7 first and needs 4.
    bal = {1: 5, 2: -5, 3: 3, 4: 4, 5: -7}
    g, o = greedy_plan(bal), optimal_plan(bal)
    assert_settles(bal, g.transfers)
    assert_settles(bal, o.transfers)
    assert len(g.transfers) == 4
    assert len(o.transfers) == 3 == oracle_min_transfers(bal)


def test_already_settled_and_two_person():
    assert optimal_plan({1: 0, 2: 0}).transfers == []
    assert optimal_plan({1: 250, 2: -250}).transfers == [Transfer(2, 1, 250)]


def test_optimal_matches_partition_oracle_random():
    rng = random.Random(11)
    for trial in range(400):
        n = rng.randint(2, 7)
        bal = random_balances(rng, n, scale=rng.choice([3, 10, 1000]), structured=trial % 2 == 0)
        o = optimal_plan(bal)
        g = greedy_plan(bal)
        assert_settles(bal, o.transfers)
        assert_settles(bal, g.transfers)
        assert len(o.transfers) == oracle_min_transfers(bal)
        assert len(o.transfers) <= len(g.transfers) <= max(0, sum(1 for v in bal.values() if v) - 1)


def test_max_zero_sum_groups_returns_real_groups():
    vals = [5, -5, 3, 4, -7, 1, -1]
    count, groups = max_zero_sum_groups(vals)
    assert count == 3 == len(groups)
    assert sorted(i for g in groups for i in g) == list(range(len(vals)))
    assert all(sum(vals[i] for i in g) == 0 for g in groups)


def test_fallback_above_limit():
    rng = random.Random(3)
    bal = random_balances(rng, MAX_EXACT + 2)
    plan = optimal_plan(bal)
    assert plan.method == "greedy_fallback"
    assert_settles(bal, plan.transfers)


def test_rejects_unbalanced():
    with pytest.raises(ValueError):
        greedy_plan({1: 5, 2: -4})
