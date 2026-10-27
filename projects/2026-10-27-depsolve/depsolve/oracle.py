"""Exhaustive reference solver for small registries (used by tests and bench)."""

from __future__ import annotations

from itertools import product

from .ranges import Range
from .registry import Registry
from .resolver import Resolver, verify
from .semver import Version


def brute_force_satisfiable(
    registry: Registry,
    root_deps: dict[str, Range],
    locked: dict[str, Version] | None = None,
) -> bool:
    helper = Resolver(registry, root_deps, locked=locked)
    names = registry.names()
    domains = [[None, *helper.allowed(n)] for n in names]
    for combo in product(*domains):
        sol = {n: v for n, v in zip(names, combo) if v is not None}
        problems = verify(registry, root_deps, sol, locked)
        # Unreachable extras are harmless for satisfiability: drop that check.
        if all("not reachable" in p for p in problems):
            return True
    return False
