"""Compile a metric query into one fan-out-safe SQLite statement.

Plan for `metrics=[revenue, orders], group_by=[customers__region]`:

  1. Expand requested metrics to *leaf* simple metrics and group them by (source dataset,
     metric-level filters). Each group becomes one CTE that aggregates at its own grain.
  2. In each CTE, join only along many-to-one edges, starting at the measure's dataset, so the
     joins can never multiply the rows being aggregated. A dimension that is only reachable by
     going "one-to-many" is rejected with a JoinPathError instead of silently double counting.
  3. Combine the CTEs on the group-by columns (a spine of their union + null-safe LEFT JOINs)
     and compute ratio / derived metrics on top of the already-aggregated leaves.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from metricsql.model import (ADDITIVE_AGGS, GRAINS, Dimension, Filter, Join, Metric, ModelError, SemanticModel,
                             parse_filter)


class QueryError(ValueError):
    """The query cannot be answered by the model."""


class JoinPathError(QueryError):
    """A dimension is not reachable through many-to-one joins (joining it would fan out)."""


@dataclass(frozen=True)
class DimensionRef:
    text: str
    dimension: Dimension | None  # None for metric_time (resolved per source dataset)
    grain: str | None


@dataclass
class Query:
    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    where: list[Filter] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)  # "name" or "-name" for descending
    limit: int | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Query":
        try:
            where = [parse_filter(f) for f in raw.get("where", [])]
        except ModelError as exc:
            raise QueryError(str(exc)) from None
        limit = raw.get("limit")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise QueryError(f"limit must be a non-negative integer, got {limit!r}")
        return cls(list(raw.get("metrics", [])), list(raw.get("group_by", [])), where,
                   list(raw.get("order_by", [])), limit)


@dataclass
class CompiledQuery:
    sql: str
    params: list
    columns: list[str]
    plan: list[str]  # human-readable notes: one line per CTE with its join path


def parse_dimension_ref(model: SemanticModel, text: str) -> DimensionRef:
    parts = text.split("__")
    if parts[0] == "metric_time":
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in GRAINS):
            raise QueryError(f"bad metric_time reference {text!r}; use metric_time__<{'|'.join(GRAINS)}>")
        return DimensionRef(text, None, parts[1] if len(parts) == 2 else "day")
    if len(parts) not in (2, 3):
        raise QueryError(f"bad dimension reference {text!r}; use dataset__dimension[__grain]")
    ds = model.datasets.get(parts[0])
    if ds is None or parts[1] not in ds.dimensions:
        raise QueryError(f"unknown dimension {text!r}")
    dim = ds.dimensions[parts[1]]
    grain = parts[2] if len(parts) == 3 else None
    if grain is not None and (dim.type != "time" or grain not in GRAINS):
        raise QueryError(f"grain {grain!r} is not valid for {dim.type} dimension {parts[0]}__{parts[1]}")
    return DimensionRef(text, dim, grain)


def grain_sql(expr: str, grain: str | None) -> str:
    if grain is None:
        return expr
    return {
        "day": f"date({expr})",
        "week": f"date({expr}, '-6 days', 'weekday 1')",  # Monday-start ISO week
        "month": f"strftime('%Y-%m-01', {expr})",
        "quarter": (f"printf('%s-%02d-01', strftime('%Y', {expr}), "
                    f"((CAST(strftime('%m', {expr}) AS INTEGER) - 1) / 3) * 3 + 1)"),
        "year": f"strftime('%Y-01-01', {expr})",
    }[grain]


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def join_tree(model: SemanticModel, base: str, targets: set[str]) -> list[Join]:
    """Many-to-one joins needed to reach `targets` from `base`, in a valid join order.

    BFS over directed many-to-one edges. If a target is reachable by two different shortest
    paths the result would depend on which path we picked, so that is an error, too.
    """
    parent: dict[str, Join | None] = {base: None}
    depth = {base: 0}
    ambiguous: set[str] = set()
    queue = deque([base])
    while queue:
        node = queue.popleft()
        for j in model.datasets[node].joins:
            if j.to not in depth:
                depth[j.to] = depth[node] + 1
                parent[j.to] = j
                queue.append(j.to)
            elif depth[j.to] == depth[node] + 1 and parent[j.to] != j:
                ambiguous.add(j.to)
    needed: dict[str, Join] = {}
    for target in sorted(targets):
        if target not in depth:
            raise JoinPathError(
                f"dataset {target!r} is not reachable from {base!r} through many-to-one joins; "
                f"joining it would repeat {base!r} rows and inflate its measures (fan-out)")
        node = target
        while parent[node] is not None:
            if node in ambiguous:
                raise JoinPathError(f"ambiguous join path from {base!r} to {node!r}")
            needed[node] = parent[node]
            node = parent[node].from_dataset
    return sorted(needed.values(), key=lambda j: (depth[j.to], j.to))


class _Params:
    def __init__(self) -> None:
        self.values: list = []

    def add(self, value) -> str:
        if isinstance(value, list):
            self.values.extend(value)
            return "(" + ", ".join("?" for _ in value) + ")"
        self.values.append(value)
        return "?"


def _filter_sql(expr: str, f: Filter, params: _Params) -> str:
    if f.op in ("is null", "is not null"):
        return f"{expr} {f.op.upper()}"
    return f"{expr} {f.op.upper()} {params.add(f.value)}"


def metric_expressions(model: SemanticModel, leaf_cte: dict[str, tuple[str, str]]):
    """Return metric_sql(name): SQL for a metric over aggregated leaf columns {leaf: (cte, agg)}."""
    exprs: dict[str, str] = {}

    def metric_sql(name: str) -> str:
        if name in exprs:
            return exprs[name]
        metric = model.metrics[name]
        if metric.type == "simple":
            cte, agg = leaf_cte[name]
            col = f"{cte}.{quote(name)}"
            sql = f"COALESCE({col}, 0)" if agg in ADDITIVE_AGGS else col
        elif metric.type == "ratio":
            sql = f"CAST({metric_sql(metric.numerator)} AS REAL) / NULLIF({metric_sql(metric.denominator)}, 0)"
        else:
            parts = [f"CAST({metric_sql(text)} AS REAL)" if kind == "metric" else text
                     for kind, text in metric.tokens]
            sql = "(" + " ".join(parts) + ")"
        exprs[name] = sql
        return sql

    return metric_sql


class Compiler:
    def __init__(self, model: SemanticModel):
        self.model = model

    # -- helpers -------------------------------------------------------------------------------

    def _leaves(self, name: str, seen: dict[str, Metric]) -> None:
        metric = self.model.metrics[name]
        if metric.type == "simple":
            seen.setdefault(name, metric)
        for dep in metric.dependencies():
            self._leaves(dep, seen)

    def _resolve_dim(self, ref: DimensionRef, base: str) -> tuple[Dimension, str | None]:
        if ref.dimension is not None:
            return ref.dimension, ref.grain
        time_ref = self.model.datasets[base].time_dimension
        if time_ref is None:
            raise QueryError(f"dataset {base!r} has no time_dimension, so metric_time is undefined for it")
        return parse_dimension_ref(self.model, time_ref).dimension, ref.grain

    @staticmethod
    def _dim_sql(dim: Dimension, grain: str | None) -> str:
        return grain_sql(dim.sql.replace("${TABLE}", quote(dim.dataset)), grain)

    def _check_query(self, query: Query) -> list[DimensionRef]:
        if not query.metrics:
            raise QueryError("query needs at least one metric")
        for name in query.metrics:
            if name not in self.model.metrics:
                raise QueryError(f"unknown metric {name!r}")
        if len(set(query.metrics)) != len(query.metrics) or len(set(query.group_by)) != len(query.group_by):
            raise QueryError("duplicate metric or group_by entry")
        dims = [parse_dimension_ref(self.model, g) for g in query.group_by]
        clash = set(query.metrics) & set(query.group_by)
        if clash:
            raise QueryError(f"name used as both metric and dimension: {sorted(clash)}")
        for f in query.where:
            parse_dimension_ref(self.model, f.dimension)
        known = set(query.metrics) | set(query.group_by)
        for item in query.order_by:
            if item.lstrip("-") not in known:
                raise QueryError(f"order_by {item!r} must name a selected metric or group_by dimension")
        return dims

    # -- main entry point ----------------------------------------------------------------------

    def compile(self, query: Query) -> CompiledQuery:
        dims = self._check_query(query)
        leaves: dict[str, Metric] = {}
        for name in query.metrics:
            self._leaves(name, leaves)

        groups: dict[tuple, list[Metric]] = {}
        for leaf in leaves.values():
            measure = self.model.measure(leaf.measure)
            key = (measure.dataset, tuple(sorted(f.key() for f in leaf.filters)))
            groups.setdefault(key, []).append(leaf)

        params = _Params()
        ctes, plan, leaf_cte = [], [], {}
        dim_cols = [quote(d.text) for d in dims]
        for idx, ((base, _), group_leaves) in enumerate(sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1]))):
            cte = f"m{idx}"
            filters = list(query.where) + list(group_leaves[0].filters)
            select, needed = [], set()
            for ref in dims:
                dim, grain = self._resolve_dim(ref, base)
                needed.add(dim.dataset)
                select.append(f"{self._dim_sql(dim, grain)} AS {quote(ref.text)}")
            where = []
            for f in filters:
                dim, grain = self._resolve_dim(parse_dimension_ref(self.model, f.dimension), base)
                needed.add(dim.dataset)
                where.append(_filter_sql(self._dim_sql(dim, grain), f, params))
            for leaf in group_leaves:
                m = self.model.measure(leaf.measure)
                expr = m.sql.replace("${TABLE}", quote(base))
                agg = f"COUNT(DISTINCT {expr})" if m.agg == "count_distinct" else f"{m.agg.upper()}({expr})"
                select.append(f"{agg} AS {quote(leaf.name)}")
                leaf_cte[leaf.name] = (cte, m.agg)
            joins = join_tree(self.model, base, needed - {base})
            ds = self.model.datasets[base]
            lines = [f"  SELECT {', '.join(select)}", f"  FROM {quote(ds.table)} AS {quote(base)}"]
            for j in joins:
                lines.append(f"  LEFT JOIN {quote(self.model.datasets[j.to].table)} AS {quote(j.to)} "
                             f"ON {quote(j.from_dataset)}.{quote(j.foreign_key)} = {quote(j.to)}.{quote(j.to_key)}")
            if where:
                lines.append("  WHERE " + " AND ".join(where))
            if dims:
                lines.append("  GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims))))
            ctes.append(f"{cte} AS (\n" + "\n".join(lines) + "\n)")
            path = " -> ".join([base] + [j.to for j in joins]) if joins else base
            plan.append(f"{cte}: {', '.join(l.name for l in group_leaves)} | from {path}"
                        + (f" | {len(filters)} filter(s)" if filters else ""))

        cte_names = [c.split(" ", 1)[0] for c in ctes]
        if dims and len(ctes) > 1:
            union = "\n  UNION\n".join(f"  SELECT {', '.join(dim_cols)} FROM {c}" for c in cte_names)
            ctes.append(f"spine AS (\n{union}\n)")
            from_sql = "FROM spine"
            for c in cte_names:
                on = " AND ".join(f"{c}.{col} IS spine.{col}" for col in dim_cols)
                from_sql += f"\nLEFT JOIN {c} ON {on}"
            dim_select = [f"spine.{col}" for col in dim_cols]
        else:
            from_sql = "FROM " + " CROSS JOIN ".join(cte_names)
            dim_select = [f"{cte_names[0]}.{col}" for col in dim_cols]

        metric_sql = metric_expressions(self.model, leaf_cte)
        select = dim_select + [f"{metric_sql(n)} AS {quote(n)}" for n in query.metrics]
        sql = "WITH " + ",\n".join(ctes) + "\nSELECT " + ",\n       ".join(select) + "\n" + from_sql
        # ORDER BY output positions: names would be ambiguous between the spine and the CTEs.
        columns = list(query.group_by) + list(query.metrics)
        order = [f"{columns.index(o.lstrip('-')) + 1} {'DESC' if o.startswith('-') else 'ASC'}"
                 for o in query.order_by]
        # Always finish with the dimensions so output order is deterministic.
        ordered = {o.lstrip("-") for o in query.order_by}
        order += [f"{i + 1} ASC" for i, d in enumerate(query.group_by) if d not in ordered]
        if order:
            sql += "\nORDER BY " + ", ".join(order)
        if query.limit is not None:
            sql += f"\nLIMIT {params.add(query.limit)}"
        return CompiledQuery(sql, params.values, columns, plan)


def compile_query(model: SemanticModel, query: Query | dict) -> CompiledQuery:
    if isinstance(query, dict):
        query = Query.from_dict(query)
    return Compiler(model).compile(query)


def run_query(conn, model: SemanticModel, query: Query | dict) -> tuple[list[str], list[tuple]]:
    compiled = compile_query(model, query)
    rows = conn.execute(compiled.sql, compiled.params).fetchall()
    return compiled.columns, rows
