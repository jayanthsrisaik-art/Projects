"""Deterministic lockfile output, lock diffs and dependency paths ("why")."""

from __future__ import annotations

from collections import deque

from .ranges import Range
from .registry import Registry
from .semver import Version, parse_version

LOCKFILE_VERSION = 1


def build_lock(
    registry: Registry,
    root_name: str,
    root_deps: dict[str, Range],
    solution: dict[str, Version],
) -> dict:
    required_by: dict[str, list[str]] = {name: [] for name in solution}
    for q, r in root_deps.items():
        required_by[q].append(f"{root_name} ({r})")
    packages = {}
    for name in sorted(solution):
        v = solution[name]
        deps = registry.get(name, v).dependencies
        for q, r in deps.items():
            required_by[q].append(f"{name}@{v} ({r})")
        packages[name] = {"version": str(v), "dependencies": {q: str(r) for q, r in sorted(deps.items())}}
    for name in packages:
        packages[name]["required_by"] = sorted(required_by[name])
    return {
        "lockfile_version": LOCKFILE_VERSION,
        "root": {"name": root_name, "dependencies": {q: str(r) for q, r in sorted(root_deps.items())}},
        "packages": packages,
    }


def diff_locks(old: dict[str, Version], new: dict[str, Version]) -> list[str]:
    lines = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        if a is None:
            lines.append(f"+ {name} {b}")
        elif b is None:
            lines.append(f"- {name} {a}")
        else:
            lines.append(f"{'↑' if b > a else '↓'} {name} {a} -> {b}")
    return lines


def why(lock: dict, target: str) -> list[str] | None:
    """Shortest dependency chain from the root to ``target`` in a lockfile."""
    packages = lock.get("packages", {})
    if target not in packages:
        return None
    root = lock.get("root", {}).get("name", "root")
    label = lambda n: f"{n}@{packages[n]['version']}"  # noqa: E731
    parent: dict[str, str | None] = {}
    queue: deque[str] = deque()
    for q in sorted(lock.get("root", {}).get("dependencies", {})):
        if q in packages and q not in parent:
            parent[q] = None
            queue.append(q)
    while queue:
        n = queue.popleft()
        if n == target:
            chain = []
            cur: str | None = n
            while cur is not None:
                chain.append(label(cur))
                cur = parent[cur]
            return [root, *reversed(chain)]
        for q in sorted(packages[n].get("dependencies", {})):
            if q in packages and q not in parent:
                parent[q] = n
                queue.append(q)
    return None


def lock_versions(lock: dict) -> dict[str, Version]:
    return {n: parse_version(m["version"]) for n, m in lock.get("packages", {}).items()}
