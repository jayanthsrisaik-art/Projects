"""Turn a failed search into a short, human-readable explanation.

Two complementary views:

* **Minimal root core** – a deletion filter over the manifest: drop each root
  requirement in turn and keep it only if the rest becomes solvable. The result
  is a set of root requirements that cannot be installed together and from which
  no member can be removed (minimal, not necessarily minimum).
* **Incompatibilities** – the empty-domain events the resolver recorded, each
  reduced to its smallest cause: a missing package, one constraint that matches
  no published version, or a pair of constraints with no version in common.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .ranges import parse_range
from .resolver import EmptyDomain, Resolution, ResolutionError, Resolver


@dataclass
class Explanation:
    core: list[str]  # "name range" root requirements
    reasons: list[str]

    def render(self) -> str:
        lines = ["Resolution failed."]
        if self.core:
            lines.append("These root requirements cannot be installed together:")
            lines += [f"  - {c}" for c in self.core]
        if self.reasons:
            lines.append("Because:")
            lines += [f"  - {r}" for r in self.reasons]
        return "\n".join(lines)


def _split(label: str, root_name: str) -> tuple[str, str]:
    # Split at the last "@" so scoped names like "@acme/web@1.0.0" stay whole;
    # the root label carries no version and may itself be scoped.
    if label == root_name:
        return label, ""
    name, _, version = label.rpartition("@")
    return name, version


def _describe(resolver: Resolver, event: EmptyDomain) -> tuple:
    """Reduce an event to a grouping key; versions are kept apart so they can merge."""
    pkg = event.package
    if pkg not in resolver.registry:
        return ("missing", pkg, tuple(sorted({o for o, _ in event.constraints})))
    allowed = resolver.allowed(pkg)
    parsed = [(o, raw, parse_range(raw)) for o, raw in event.constraints]
    for o, raw, r in parsed:
        if not r.filter(allowed):
            return ("none", pkg, o, raw, ", ".join(str(v) for v in allowed) or "none")
    for (o1, raw1, r1), (o2, raw2, r2) in combinations(parsed, 2):
        if not [v for v in allowed if r1.satisfies(v) and r2.satisfies(v)]:
            root = resolver.root_name
            (n1, v1), (n2, v2) = sorted([(_split(o1, root), raw1), (_split(o2, root), raw2)])
            return ("pair", pkg, n1[0], n2[0], (n1[1], v1), (n2[1], v2))
    return ("all", pkg, tuple(f"{o} requires {raw}" for o, raw, _ in parsed))


def _side(name: str, items: set[tuple[str, str]]) -> str:
    versions = "/".join(sorted({v for v, _ in items if v}))
    ranges = "/".join(sorted({r for _, r in items}))
    return f"{name}{'@' + versions if versions else ''} requires {{pkg}} {ranges}"


def _render(key: tuple, merged: dict) -> str:
    kind, pkg = key[0], key[1]
    if kind == "missing":
        return f"{pkg} is not in the registry (required by {', '.join(key[2])})"
    if kind == "none":
        return f"{key[2]} requires {pkg} {key[3]}, but no installable version matches (available: {key[4]})"
    if kind == "pair":
        a, b = merged[key]
        left = _side(key[2], a).format(pkg=pkg)
        right = _side(key[3], b).format(pkg=pkg)
        return f"{left}, but {right}; no version of {pkg} satisfies both"
    return f"no version of {pkg} satisfies all of: {'; '.join(key[2])}"


def _solvable(resolver: Resolver, names: list[str], max_steps: int) -> bool | None:
    probe = Resolver(
        resolver.registry,
        {n: resolver.root_deps[n] for n in names},
        locked=resolver.locked,
        upgrade=resolver.upgrade,
        max_steps=max_steps,
        root_name=resolver.root_name,
    )
    try:
        return probe.resolve().ok
    except ResolutionError:
        return None  # unknown: treat as "still failing" so the requirement is kept


def explain(resolver: Resolver, result: Resolution, *, max_reasons: int = 5, probe_steps: int = 20_000) -> Explanation:
    assert not result.ok
    core = sorted(resolver.root_deps)
    for name in list(core):
        rest = [n for n in core if n != name]
        if _solvable(resolver, rest, probe_steps) is False:
            core = rest
    # Re-run on the core so the reasons describe the essential conflict only.
    core_resolver = Resolver(
        resolver.registry,
        {n: resolver.root_deps[n] for n in core},
        locked=resolver.locked,
        upgrade=resolver.upgrade,
        max_steps=resolver.max_steps,
        root_name=resolver.root_name,
    )
    try:
        events = core_resolver.resolve().empty_domains
    except ResolutionError:
        core_resolver, events = resolver, result.empty_domains
    counts: dict[tuple, int] = {}
    merged: dict[tuple, tuple[set, set]] = {}
    for event, n in events.items():
        key = _describe(core_resolver, event)
        if key[0] == "pair":
            side_a, side_b = key[4], key[5]
            key = key[:4]
            a, b = merged.setdefault(key, (set(), set()))
            a.add(side_a)
            b.add(side_b)
        counts[key] = counts.get(key, 0) + n
    ordered = sorted(counts, key=lambda k: (-counts[k], repr(k)))[:max_reasons]
    reasons = [_render(k, merged) for k in ordered]
    return Explanation([f"{n} {resolver.root_deps[n]}" for n in core], reasons)
