"""Baseline: what a hand-written "join everything, then GROUP BY" query computes.

It joins every dataset the query touches into one wide row set (following join edges in either
direction) and aggregates once. Whenever that walks a one-to-many edge, measures on the "one"
side are repeated per child row and get inflated. Included only to measure that error against
the fan-out-safe compiler.
"""

from __future__ import annotations

from collections import deque

from metricsql.compiler import (Compiler, CompiledQuery, Query, _filter_sql, _Params, metric_expressions,
                                parse_dimension_ref, quote)
from metricsql.model import SemanticModel


def compile_naive(model: SemanticModel, query: Query | dict) -> CompiledQuery:
    if isinstance(query, dict):
        query = Query.from_dict(query)
    comp = Compiler(model)
    dims = comp._check_query(query)
    leaves: dict = {}
    for name in query.metrics:
        comp._leaves(name, leaves)
    root = model.measure(next(iter(leaves.values())).measure).dataset

    needed = {model.measure(l.measure).dataset for l in leaves.values()}
    resolved_dims = [comp._resolve_dim(d, root) for d in dims]
    where_dims = [(comp._resolve_dim(parse_dimension_ref(model, f.dimension), root), f) for f in query.where]
    needed |= {d.dataset for d, _ in resolved_dims} | {d.dataset for (d, _), _ in where_dims}
    for leaf in leaves.values():
        for f in leaf.filters:
            needed.add(comp._resolve_dim(parse_dimension_ref(model, f.dimension), root)[0].dataset)

    # Undirected BFS from the root over all join edges.
    edges: dict[str, list[tuple[str, str]]] = {n: [] for n in model.datasets}
    for ds in model.datasets.values():
        for j in ds.joins:
            on = f"{quote(j.from_dataset)}.{quote(j.foreign_key)} = {quote(j.to)}.{quote(j.to_key)}"
            edges[j.from_dataset].append((j.to, on))
            edges[j.to].append((j.from_dataset, on))
    via: dict[str, tuple[str, str] | None] = {root: None}
    order, queue = [], deque([root])
    while queue:
        node = queue.popleft()
        for nxt, on in edges[node]:
            if nxt not in via:
                via[nxt] = (node, on)
                order.append(nxt)
                queue.append(nxt)
    use = set()
    for target in needed:
        node = target
        while node != root:
            use.add(node)
            node = via[node][0]

    params = _Params()
    select = [f"{comp._dim_sql(d, g)} AS {quote(ref.text)}" for (d, g), ref in zip(resolved_dims, dims)]
    leaf_cte = {}
    for leaf in leaves.values():
        m = model.measure(leaf.measure)
        expr = m.sql.replace("${TABLE}", quote(m.dataset))
        if leaf.filters:
            conds = [_filter_sql(comp._dim_sql(*comp._resolve_dim(parse_dimension_ref(model, f.dimension), root)), f, params)
                     for f in leaf.filters]
            expr = f"CASE WHEN {' AND '.join(conds)} THEN {expr} END"
        agg = f"COUNT(DISTINCT {expr})" if m.agg == "count_distinct" else f"{m.agg.upper()}({expr})"
        select.append(f"{agg} AS {quote(leaf.name)}")
        leaf_cte[leaf.name] = ("m0", m.agg)
    lines = [f"  SELECT {', '.join(select)}", f"  FROM {quote(model.datasets[root].table)} AS {quote(root)}"]
    for node in order:
        if node in use:
            lines.append(f"  LEFT JOIN {quote(model.datasets[node].table)} AS {quote(node)} ON {via[node][1]}")
    if where_dims:
        lines.append("  WHERE " + " AND ".join(_filter_sql(comp._dim_sql(d, g), f, params) for (d, g), f in where_dims))
    if dims:
        lines.append("  GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims))))
    metric_sql = metric_expressions(model, leaf_cte)
    cols = [f"m0.{quote(d.text)}" for d in dims] + [f"{metric_sql(n)} AS {quote(n)}" for n in query.metrics]
    sql = "WITH m0 AS (\n" + "\n".join(lines) + "\n)\nSELECT " + ", ".join(cols) + "\nFROM m0"
    if dims:
        sql += "\nORDER BY " + ", ".join(str(i + 1) for i in range(len(dims)))
    return CompiledQuery(sql, params.values, list(query.group_by) + list(query.metrics),
                         [f"naive: one join of {', '.join([root] + sorted(use))}"])
