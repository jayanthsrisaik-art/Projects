"""Backfill planning: minimal invalidation closure + scheduling."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .partitions import ALL
from .spec import Node, Pipeline


@dataclass
class Plan:
    roots: list[Node]
    tasks: list[Node]            # derived partitions to recompute, topo-ordered
    reason: dict[Node, Node]     # task -> the upstream node that invalidated it
    level: dict[Node, int]       # 1-based wave number (longest in-plan dep chain)

    @property
    def waves(self) -> int:
        return max(self.level.values(), default=0)

    def by_table(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for t, k in self.tasks:
            out.setdefault(t, []).append(k)
        return out


def forward_deps(p: Pipeline, node: Node) -> list[Node]:
    table, key = node
    t = p.tables[table]
    out: list[Node] = []
    for d in t.deps:
        in_grain = p.grain(d.table)
        for k in d.mapping.forward(p.cal, t.grain, in_grain, key):
            out.append((d.table, k))
    return out


def plan(p: Pipeline, roots) -> Plan:
    """Breadth-first walk of inverse mappings from the changed partitions."""
    roots = p.sort_nodes(set(roots))
    seen: set[Node] = set(roots)
    reason: dict[Node, Node] = {}
    q = deque(roots)
    while q:
        node = q.popleft()
        up, key = node
        for out_table, mapping in p.downstream[up]:
            og, ig = p.grain(out_table), p.grain(up)
            for k in mapping.inverse(p.cal, og, ig, key):
                child = (out_table, k)
                if child not in seen:
                    seen.add(child)
                    reason[child] = node
                    q.append(child)
    tasks = p.sort_nodes(n for n in seen if not p.tables[n[0]].source)
    taskset = set(tasks)
    level: dict[Node, int] = {}
    for n in tasks:  # topo order guarantees in-plan deps are levelled first
        level[n] = 1 + max((level[d] for d in forward_deps(p, n) if d in taskset), default=0)
    return Plan(roots, tasks, reason, level)


def brute_force_affected(p: Pipeline, roots) -> set[Node]:
    """Oracle: every derived node whose forward-dependency closure hits a root."""
    roots = set(roots)
    memo: dict[Node, bool] = {}
    for node in p.all_nodes(derived_only=True):  # topo order => deps memoised first
        memo[node] = node in roots or any(
            d in roots or memo.get(d, False) for d in forward_deps(p, node)
        )
    return {n for n, hit in memo.items() if hit}


def explain(pl: Plan, node: Node) -> list[Node]:
    """Shortest invalidation chain from a root to ``node`` (inclusive)."""
    chain = [node]
    while chain[-1] in pl.reason:
        chain.append(pl.reason[chain[-1]])
    return list(reversed(chain))


# ---------------------------------------------------------------------------
# Baseline strategies used for comparison in the demo
# ---------------------------------------------------------------------------
def downstream_tables(p: Pipeline, roots) -> set[str]:
    tabs = {t for t, _ in roots}
    for t in p.order:  # topo order: one pass is enough
        if any(d.table in tabs for d in p.tables[t].deps):
            tabs.add(t)
    return tabs


def strategy_full(p: Pipeline, roots) -> list[Node]:
    """Rebuild every partition of every derived table."""
    return p.all_nodes(derived_only=True)


def strategy_from_earliest(p: Pipeline, roots) -> list[Node]:
    """Common practice: rebuild downstream tables from the earliest changed date."""
    starts = [p.cal.start_day(p.grain(t), k) if k != ALL else p.cal.start.isoformat()
              for t, k in roots]
    earliest = min(starts)
    tabs = downstream_tables(p, roots)
    out = []
    for t, k in p.all_nodes(derived_only=True):
        if t in tabs and p.cal.bounds(p.grain(t), k)[1] >= earliest:
            out.append((t, k))
    out += [r for r in roots if not p.tables[r[0]].source]
    return p.sort_nodes(set(out))


def strategy_same_key(p: Pipeline, roots) -> list[Node]:
    """Naive: rebuild only the *same* dates in downstream tables (often wrong)."""
    days, months, everything = set(), set(), False
    for t, k in roots:
        if k == ALL:
            everything = True
        elif len(k) == 10:
            days.add(k)
            months.add(k[:7])
        else:
            months.add(k)
            days.update(p.cal.days_in_month(k))
    tabs = downstream_tables(p, roots)
    out = []
    for t, k in p.all_nodes(derived_only=True):
        if t not in tabs:
            continue
        g = p.grain(t)
        if everything or g == "none" or (g == "day" and k in days) or (g == "month" and k in months):
            out.append((t, k))
    out += [r for r in roots if not p.tables[r[0]].source]
    return p.sort_nodes(set(out))
