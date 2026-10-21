import math

import pytest

from ratelimit.algorithms import ALGORITHMS, FixedWindow, SlidingWindowCounter, SlidingWindowLog, TokenBucket, make_limiter
from ratelimit.simulate import boundary_burst, run


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_allows_up_to_limit_then_rejects(name):
    lim = make_limiter(name, 3, 10)
    results = [lim.check("a", 0.0).allowed for _ in range(5)]
    assert results == [True, True, True, False, False]


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_keys_are_independent(name):
    lim = make_limiter(name, 1, 10)
    assert lim.check("a", 0).allowed
    assert not lim.check("a", 0).allowed
    assert lim.check("b", 0).allowed


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_remaining_counts_down(name):
    lim = make_limiter(name, 3, 10)
    assert [lim.check("a", 0).remaining for _ in range(3)] == [2, 1, 0]


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_oversized_cost_never_allowed(name):
    d = make_limiter(name, 3, 10).check("a", 0, cost=4)
    assert not d.allowed and math.isinf(d.retry_after)


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_invalid_args(name):
    with pytest.raises(ValueError):
        make_limiter(name, 0, 10)
    with pytest.raises(ValueError):
        make_limiter(name, 1, 10).check("a", 0, cost=0)


def test_unknown_algorithm():
    with pytest.raises(ValueError, match="unknown algorithm"):
        make_limiter("nope", 1, 1)


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_retry_after_is_honest(name):
    """Waiting exactly retry_after (no other traffic) must make the next request succeed."""
    lim = make_limiter(name, 4, 10)
    t = 3.0
    for i in range(4):
        assert lim.check("a", t + i * 0.5).allowed
    t = 5.0
    d = lim.check("a", t)
    assert not d.allowed and 0 < d.retry_after < math.inf
    assert lim.check("a", t + d.retry_after + 1e-6).allowed


def test_retry_after_honest_across_window_boundary_for_counter():
    lim = SlidingWindowCounter(4, 10)
    for t in (1.0, 2.0, 3.0, 4.0):
        assert lim.check("a", t).allowed
    for t in (11.0, 12.0):  # second window, previous weight still high
        lim.check("a", t)
    d = lim.check("a", 13.0)
    if not d.allowed:
        assert lim.check("a", 13.0 + d.retry_after + 1e-6).allowed


def test_fixed_window_resets_at_boundary():
    lim = FixedWindow(2, 10)
    assert lim.check("a", 9.0).allowed and lim.check("a", 9.5).allowed
    assert not lim.check("a", 9.9).allowed
    assert lim.check("a", 10.0).allowed


def test_fixed_window_edge_burst_is_double():
    lim = FixedWindow(10, 10)
    accepted = [t for t in boundary_burst() if lim.check("a", t).allowed]
    assert len(accepted) == 20


def test_sliding_log_is_exact_at_boundary():
    lim = SlidingWindowLog(10, 10)
    accepted = [t for t in boundary_burst() if lim.check("a", t).allowed]
    assert len(accepted) == 10


def test_sliding_log_expiry_is_exclusive_of_window_edge():
    lim = SlidingWindowLog(1, 10)
    assert lim.check("a", 0).allowed
    assert not lim.check("a", 9.999).allowed
    assert lim.check("a", 10.0).allowed


def test_sliding_log_weighted_cost_retry():
    lim = SlidingWindowLog(5, 10)
    assert lim.check("a", 0, cost=2).allowed
    assert lim.check("a", 4, cost=3).allowed
    d = lim.check("a", 5, cost=2)
    assert not d.allowed
    assert d.retry_after == pytest.approx(5.0)  # the cost-2 entry expires at t=10


def test_token_bucket_burst_then_refill():
    lim = TokenBucket(5, 10)  # 0.5 tokens/s
    assert all(lim.check("a", 0).allowed for _ in range(5))
    d = lim.check("a", 0)
    assert not d.allowed and d.retry_after == pytest.approx(2.0)
    assert not lim.check("a", 1.9).allowed
    assert lim.check("a", 2.0).allowed


def test_token_bucket_does_not_overfill():
    lim = TokenBucket(2, 10)
    assert lim.check("a", 0).allowed
    assert lim.check("a", 1000).remaining == 1  # capped at capacity, then one spent


def test_token_bucket_tolerates_clock_going_backwards():
    lim = TokenBucket(2, 10)
    lim.check("a", 100)
    assert lim.check("a", 50).allowed  # no negative refill / crash


def test_counter_bounds_boundary_burst():
    lim = SlidingWindowCounter(10, 10)
    accepted = [t for t in boundary_burst() if lim.check("a", t).allowed]
    assert len(accepted) < 20
    assert len(accepted) <= 12


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_evict_idle_frees_memory_without_changing_behaviour(name):
    lim = make_limiter(name, 2, 10)
    for k in "abc":
        lim.check(k, 0)
    assert len(lim) == 3
    assert lim.evict_idle(1000.0) == 3
    assert len(lim) == 0
    assert lim.check("a", 1000.0).remaining == 1


def test_evict_keeps_active_keys():
    lim = SlidingWindowLog(2, 10)
    lim.check("old", 0)
    lim.check("new", 95)
    lim.evict_idle(100)
    assert len(lim) == 1


def test_simulation_expected_values():
    r = run()
    b = r["boundary_burst"]
    assert b["fixed_window"] == {"accepted": 20, "max_in_any_10s": 20}
    assert b["sliding_log"]["max_in_any_10s"] <= 10
    # a full bucket can burst `limit` and also refill ~1 token during the burst
    assert b["token_bucket"]["max_in_any_10s"] <= 11
    s = r["steady_overload"]
    assert s["sliding_log"]["accepted"] == 30
