"""Baseline: greedy breadth-first resolution without backtracking.

Each package is fixed to the newest allowed version that satisfies the first
requirement seen for it. A later requirement that disagrees is a hard failure.
This mirrors naive installers and is what the real resolver is measured against.
"""

from __future__ import annotations

from collections import deque

from .ranges import Range
from .registry import Registry
from .resolver import Resolution, Resolver, Stats
from .semver import Version


def greedy_resolve(
    registry: Registry,
    root_deps: dict[str, Range],
    *,
    locked: dict[str, Version] | None = None,
    upgrade=(),
) -> Resolution:
    helper = Resolver(registry, root_deps, locked=locked, upgrade=upgrade)
    stats = Stats()
    chosen: dict[str, Version] = {}
    queue = deque(sorted(root_deps.items()))
    while queue:
        pkg, rng = queue.popleft()
        if pkg in chosen:
            if not rng.satisfies(chosen[pkg]):
                return Resolution(False, {}, stats)
            continue
        candidates = rng.filter(helper.allowed(pkg))
        stats.steps += 1
        if not candidates:
            return Resolution(False, {}, stats)
        chosen[pkg] = candidates[0]
        queue.extend(sorted(registry.get(pkg, candidates[0]).dependencies.items()))
    return Resolution(True, chosen, stats)
