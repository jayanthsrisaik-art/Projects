"""Backtracking dependency resolver with conflict-directed backjumping.

The search treats every package as a variable whose domain is its published
versions. A package becomes *pending* when the root or an already-chosen
release depends on it. At each step the resolver picks the pending package with
the fewest remaining candidates (MRV), tries candidates in preference order
(locked version first, then newest first), and forward-checks the chosen
release's dependencies against current choices.

When a subtree fails it reports a *conflict set*: the chosen packages whose
decisions caused the failure. If the package being decided is not in that
set, trying its other versions cannot help, so the resolver jumps straight
back to the most recent package that is (backjumping). With ``backjump=False``
it falls back to plain chronological backtracking, which is kept for the
benchmark.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .ranges import Range
from .registry import Registry
from .semver import Version

ROOT = None  # origin marker for requirements that come from the manifest


class ResolutionError(RuntimeError):
    """Raised when the search exceeds its step budget."""


@dataclass
class Stats:
    steps: int = 0  # candidate versions tried
    backtracks: int = 0  # failed candidates followed by trying a sibling
    backjumps: int = 0  # failures that skipped remaining siblings


@dataclass(frozen=True)
class EmptyDomain:
    """A moment in the search where some package had no viable version."""

    package: str
    constraints: tuple[tuple[str, str], ...]  # (origin label, range text)


@dataclass
class Resolution:
    ok: bool
    solution: dict[str, Version]
    stats: Stats
    empty_domains: Counter = field(default_factory=Counter)


class Resolver:
    def __init__(
        self,
        registry: Registry,
        root_deps: dict[str, Range],
        *,
        locked: dict[str, Version] | None = None,
        upgrade: Iterable[str] = (),
        backjump: bool = True,
        max_steps: int = 200_000,
        root_name: str = "root",
    ) -> None:
        self.registry = registry
        self.root_deps = dict(root_deps)
        self.locked = dict(locked or {})
        self.upgrade = set(upgrade)
        self.backjump = backjump
        self.max_steps = max_steps
        self.root_name = root_name
        self.stats = Stats()
        self.assign: dict[str, Version] = {}
        self.reqs: dict[str, list[tuple[str | None, Range]]] = {}
        self.empty_domains: Counter = Counter()
        self._allowed_cache: dict[str, list[Version]] = {}

    # -- domains -----------------------------------------------------------

    def allowed(self, pkg: str) -> list[Version]:
        """Versions the resolver may pick, in preference order."""
        if pkg not in self._allowed_cache:
            lock = self.locked.get(pkg) if pkg not in self.upgrade else None
            versions = self.registry.versions(pkg)
            ok = [v for v in versions if v == lock or not self.registry.get(pkg, v).yanked]
            if lock is not None and lock in ok:
                ok.remove(lock)
                ok.insert(0, lock)
            self._allowed_cache[pkg] = ok
        return self._allowed_cache[pkg]

    def _candidates(self, pkg: str, extra: Range | None = None) -> list[Version]:
        ranges = [r for _, r in self.reqs.get(pkg, ())]
        if extra is not None:
            ranges.append(extra)
        return [v for v in self.allowed(pkg) if all(r.satisfies(v) for r in ranges)]

    def _origins(self, pkg: str) -> frozenset[str]:
        return frozenset(o for o, _ in self.reqs.get(pkg, ()) if o is not ROOT)

    def _label(self, origin: str | None) -> str:
        return self.root_name if origin is ROOT else f"{origin}@{self.assign[origin]}"

    def _record_empty(self, pkg: str, extra: tuple[str, Version, Range] | None = None) -> None:
        cons = [(self._label(o), r.raw) for o, r in self.reqs.get(pkg, ())]
        if extra is not None:
            cons.append((f"{extra[0]}@{extra[1]}", extra[2].raw))
        self.empty_domains[EmptyDomain(pkg, tuple(sorted(set(cons))))] += 1

    # -- state changes -------------------------------------------------------

    def _assign(self, pkg: str, v: Version) -> None:
        self.assign[pkg] = v
        for q, r in self.registry.get(pkg, v).dependencies.items():
            if q != pkg:
                self.reqs.setdefault(q, []).append((pkg, r))

    def _unassign(self, pkg: str, v: Version) -> None:
        for q in self.registry.get(pkg, v).dependencies:
            if q == pkg:
                continue
            lst = self.reqs[q]
            for i in range(len(lst) - 1, -1, -1):
                if lst[i][0] == pkg:
                    del lst[i]
                    break
            if not lst:
                del self.reqs[q]
        del self.assign[pkg]

    # -- search ----------------------------------------------------------------

    def _check_value(self, pkg: str, v: Version) -> frozenset[str] | None:
        """Forward check: None if pkg@v is consistent, else the conflict set."""
        for q, r in self.registry.get(pkg, v).dependencies.items():
            if q == pkg:
                if not r.satisfies(v):
                    return frozenset()
            elif q in self.assign:
                if not r.satisfies(self.assign[q]):
                    # q's chosen version has to give way: record q as out of options.
                    self._record_empty(q, extra=(pkg, v, r))
                    return frozenset({q})
            elif not self._candidates(q, extra=r):
                self._record_empty(q, extra=(pkg, v, r))
                return self._origins(q)
        return None

    def _search(self) -> tuple[bool, frozenset[str]]:
        pending = sorted(p for p in self.reqs if p not in self.assign)
        if not pending:
            return True, frozenset()
        pkg, cands = None, None
        for p in pending:
            c = self._candidates(p)
            if cands is None or len(c) < len(cands):
                pkg, cands = p, c
                if not c:
                    break
        assert pkg is not None and cands is not None
        origins = self._origins(pkg)
        if not cands:
            self._record_empty(pkg)
            return False, origins

        conflict: set[str] = set()
        for v in cands:
            self.stats.steps += 1
            if self.stats.steps > self.max_steps:
                raise ResolutionError(f"step budget of {self.max_steps} exceeded")
            bad = self._check_value(pkg, v)
            if bad is not None:
                conflict |= bad
                continue
            self._assign(pkg, v)
            ok, sub = self._search()
            if ok:
                return True, frozenset()
            self._unassign(pkg, v)
            if self.backjump and pkg not in sub:
                self.stats.backjumps += 1
                return False, sub
            conflict |= sub - {pkg}
            self.stats.backtracks += 1
        return False, frozenset(conflict | origins)

    def resolve(self) -> Resolution:
        self.reqs = {name: [(ROOT, r)] for name, r in self.root_deps.items()}
        self.assign = {}
        self.stats = Stats()
        self.empty_domains = Counter()
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, 4 * len(self.registry.names()) + 1000))
        try:
            ok, _ = self._search()
        finally:
            sys.setrecursionlimit(limit)
        return Resolution(ok, dict(self.assign) if ok else {}, self.stats, self.empty_domains)


def resolve(registry: Registry, root_deps: dict[str, Range], **kwargs) -> Resolution:
    return Resolver(registry, root_deps, **kwargs).resolve()


def verify(
    registry: Registry,
    root_deps: dict[str, Range],
    solution: dict[str, Version],
    locked: dict[str, Version] | None = None,
) -> list[str]:
    """Independent check of a solution; returns human-readable violations."""
    locked = locked or {}
    problems: list[str] = []
    for name, v in solution.items():
        if name not in registry or v not in registry.versions(name):
            problems.append(f"{name}@{v} is not published")
            continue
        if registry.get(name, v).yanked and locked.get(name) != v:
            problems.append(f"{name}@{v} is yanked and not locked")
    edges: list[tuple[str, str, Range]] = [("<root>", q, r) for q, r in root_deps.items()]
    for name, v in solution.items():
        if name in registry and v in registry.versions(name):
            edges += [(f"{name}@{v}", q, r) for q, r in registry.get(name, v).dependencies.items()]
    for origin, q, r in edges:
        if q not in solution:
            problems.append(f"{origin} requires {q} {r} but it is missing")
        elif not r.satisfies(solution[q]):
            problems.append(f"{origin} requires {q} {r} but {q}@{solution[q]} was chosen")
    # Every installed package must be reachable from the root.
    seen, stack = set(), [q for q in root_deps if q in solution]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        if p in registry and solution[p] in registry.versions(p):
            stack += [q for q in registry.get(p, solution[p]).dependencies if q in solution]
    for extra in sorted(set(solution) - seen):
        problems.append(f"{extra}@{solution[extra]} is not reachable from the root")
    return problems
