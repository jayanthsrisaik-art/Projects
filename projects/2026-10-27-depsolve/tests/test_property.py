"""Randomised cross-check against an exhaustive solver."""

import random

import pytest

from depsolve.greedy import greedy_resolve
from depsolve.oracle import brute_force_satisfiable
from depsolve.resolver import Resolver, verify

from helpers import random_problem

N = 250


@pytest.mark.parametrize("seed", range(N))
def test_matches_brute_force(seed):
    reg, root, locked = random_problem(random.Random(seed))
    expected = brute_force_satisfiable(reg, root, locked)
    cbj = Resolver(reg, root, locked=locked).resolve()
    chrono = Resolver(reg, root, locked=locked, backjump=False).resolve()
    assert cbj.ok is expected
    assert chrono.ok is expected
    if expected:
        assert verify(reg, root, cbj.solution, locked) == []
        # Both strategies try candidates in the same order, so the first solution matches.
        assert cbj.solution == chrono.solution
    greedy = greedy_resolve(reg, root, locked=locked)
    if greedy.ok:  # greedy may miss solutions but must never return an invalid one
        assert expected and verify(reg, root, greedy.solution, locked) == []
