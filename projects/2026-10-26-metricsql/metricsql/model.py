"""Semantic model: datasets (tables), dimensions, measures, many-to-one joins and metrics.

The model is authored as JSON by a trusted analytics engineer, so dimension and measure `sql`
fragments are taken as written. Everything a *query* supplies is resolved against the model by
name and never pasted into SQL (filter values become bound parameters).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")  # no "__": it separates reference parts
AGGS = ("sum", "count", "count_distinct", "avg", "min", "max")
ADDITIVE_AGGS = ("sum", "count", "count_distinct")  # empty group means 0, not NULL
GRAINS = ("day", "week", "month", "quarter", "year")
FILTER_OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "not in", "is null", "is not null")
METRIC_TYPES = ("simple", "ratio", "derived")
DERIVED_TOKEN_RE = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([a-z][a-z0-9_]*)|([-+*/()]))")


class ModelError(ValueError):
    """The semantic model JSON is invalid."""


@dataclass(frozen=True)
class Dimension:
    name: str
    dataset: str
    sql: str  # uses ${TABLE} for the dataset alias
    type: str = "categorical"  # or "time" (ISO date / datetime text)


@dataclass(frozen=True)
class Measure:
    name: str
    dataset: str
    agg: str
    sql: str


@dataclass(frozen=True)
class Join:
    """A many-to-one edge: each row of `from_dataset` matches at most one row of `to`."""

    from_dataset: str
    to: str
    foreign_key: str
    to_key: str


@dataclass(frozen=True)
class Filter:
    dimension: str  # a dimension reference, e.g. "orders__status" or "metric_time__month"
    op: str
    value: object = None

    def key(self) -> tuple:
        value = tuple(self.value) if isinstance(self.value, list) else self.value
        return (self.dimension, self.op, repr(value))


@dataclass
class Dataset:
    name: str
    table: str
    primary_key: str
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    joins: list[Join] = field(default_factory=list)
    time_dimension: str | None = None  # dimension reference used for metric_time


@dataclass
class Metric:
    name: str
    type: str
    measure: str | None = None  # "dataset.measure" for simple metrics
    filters: tuple[Filter, ...] = ()
    numerator: str | None = None
    denominator: str | None = None
    expr: str | None = None
    tokens: tuple[tuple[str, str], ...] = ()  # parsed derived expression: (kind, text)
    description: str = ""

    def dependencies(self) -> list[str]:
        if self.type == "ratio":
            return [self.numerator, self.denominator]
        if self.type == "derived":
            return [text for kind, text in self.tokens if kind == "metric"]
        return []


def parse_filter(raw: dict) -> Filter:
    if not isinstance(raw, dict) or "dimension" not in raw or "op" not in raw:
        raise ModelError(f"filter must be an object with 'dimension' and 'op': {raw!r}")
    op = str(raw["op"]).lower()
    if op not in FILTER_OPS:
        raise ModelError(f"unknown filter op {raw['op']!r}; expected one of {', '.join(FILTER_OPS)}")
    value = raw.get("value")
    if op in ("in", "not in"):
        if not isinstance(value, list) or not value:
            raise ModelError(f"op {op!r} needs a non-empty list value")
        items = value
    elif op in ("is null", "is not null"):
        if value is not None:
            raise ModelError(f"op {op!r} takes no value")
        items = []
    else:
        items = [value]
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ModelError(f"filter values must be strings or numbers, got {item!r}")
    return Filter(str(raw["dimension"]), op, value)


def tokenize_derived(expr: str) -> tuple[tuple[str, str], ...]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    expr = expr.rstrip()
    while pos < len(expr):
        m = DERIVED_TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise ModelError(f"cannot parse derived expression {expr!r} at position {pos}")
        number, name, op = m.groups()
        tokens.append(("number", number) if number else ("metric", name) if name else ("op", op))
        pos = m.end()
    # Tiny grammar check: operands and binary operators must alternate; '(' and unary '-' may
    # start an operand, ')' may end one. This keeps arbitrary SQL out of derived metrics.
    depth, expect_operand = 0, True
    for kind, text in tokens:
        if expect_operand:
            if kind in ("number", "metric"):
                expect_operand = False
            elif text == "(":
                depth += 1
            elif text != "-":
                break
        else:
            if text == ")":
                depth -= 1
                if depth < 0:
                    break
            elif kind == "op" and text != "(":
                expect_operand = True
            else:
                break
    else:
        if tokens and depth == 0 and not expect_operand:
            return tuple(tokens)
    raise ModelError(f"invalid derived expression {expr!r}")


def _name(value: str, what: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ModelError(f"invalid {what} name {value!r}: use lower_snake_case without '__'")
    return value


def _column_sql(raw: dict, what: str) -> str:
    if "sql" in raw:
        return str(raw["sql"])
    if "column" in raw:
        return f'${{TABLE}}."{_name(raw["column"], "column")}"'
    raise ModelError(f"{what} needs 'column' or 'sql'")


class SemanticModel:
    def __init__(self, datasets: dict[str, Dataset], metrics: dict[str, Metric]):
        self.datasets = datasets
        self.metrics = metrics

    # -- loading -------------------------------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict) -> "SemanticModel":
        if not isinstance(raw.get("datasets"), dict) or not raw["datasets"]:
            raise ModelError("model needs a non-empty 'datasets' object")
        datasets: dict[str, Dataset] = {}
        for ds_name, ds_raw in raw["datasets"].items():
            _name(ds_name, "dataset")
            ds = Dataset(
                name=ds_name,
                table=_name(ds_raw.get("table", ds_name), "table"),
                primary_key=_name(ds_raw.get("primary_key", ""), "primary_key"),
                time_dimension=ds_raw.get("time_dimension"),
            )
            for dim_name, dim_raw in ds_raw.get("dimensions", {}).items():
                dim_type = dim_raw.get("type", "categorical")
                if dim_type not in ("categorical", "time"):
                    raise ModelError(f"dimension {ds_name}.{dim_name}: unknown type {dim_type!r}")
                ds.dimensions[_name(dim_name, "dimension")] = Dimension(
                    dim_name, ds_name, _column_sql(dim_raw, f"dimension {ds_name}.{dim_name}"), dim_type)
            for m_name, m_raw in ds_raw.get("measures", {}).items():
                agg = m_raw.get("agg")
                if agg not in AGGS:
                    raise ModelError(f"measure {ds_name}.{m_name}: agg must be one of {', '.join(AGGS)}")
                ds.measures[_name(m_name, "measure")] = Measure(
                    m_name, ds_name, agg, _column_sql(m_raw, f"measure {ds_name}.{m_name}"))
            for j in ds_raw.get("joins", []):
                ds.joins.append(Join(ds_name, _name(j.get("to", ""), "join target"),
                                     _name(j.get("foreign_key", ""), "foreign_key"), j.get("to_key", "")))
            datasets[ds_name] = ds

        for ds in datasets.values():
            fixed = []
            for j in ds.joins:
                if j.to not in datasets:
                    raise ModelError(f"dataset {ds.name} joins unknown dataset {j.to!r}")
                if j.to == ds.name:
                    raise ModelError(f"dataset {ds.name} joins itself")
                fixed.append(Join(j.from_dataset, j.to, j.foreign_key,
                                  _name(j.to_key or datasets[j.to].primary_key, "to_key")))
            if len({j.to for j in fixed}) != len(fixed):
                raise ModelError(f"dataset {ds.name} has two joins to the same dataset")
            ds.joins = fixed

        metrics: dict[str, Metric] = {}
        for name, m_raw in raw.get("metrics", {}).items():
            _name(name, "metric")
            if name == "metric_time":
                raise ModelError("'metric_time' is reserved")
            mtype = m_raw.get("type", "simple")
            if mtype not in METRIC_TYPES:
                raise ModelError(f"metric {name}: type must be one of {', '.join(METRIC_TYPES)}")
            metric = Metric(name, mtype, description=m_raw.get("description", ""))
            if mtype == "simple":
                metric.measure = m_raw.get("measure")
                metric.filters = tuple(parse_filter(f) for f in m_raw.get("filter", []))
            elif mtype == "ratio":
                metric.numerator, metric.denominator = m_raw.get("numerator"), m_raw.get("denominator")
            else:
                metric.expr = m_raw.get("expr", "")
                metric.tokens = tokenize_derived(metric.expr)
            metrics[name] = metric

        model = cls(datasets, metrics)
        model._validate()
        return model

    @classmethod
    def load(cls, path: str | Path) -> "SemanticModel":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def _validate(self) -> None:
        try:
            self._validate_refs()
        except ValueError as exc:  # QueryError from reference parsing is a model error here
            raise ModelError(str(exc)) from None

    def _validate_refs(self) -> None:
        from metricsql.compiler import parse_dimension_ref  # local import: compiler depends on model

        for ds in self.datasets.values():
            if ds.time_dimension is not None:
                ref = parse_dimension_ref(self, ds.time_dimension)
                if ref.dimension.type != "time" or ref.grain is not None:
                    raise ModelError(f"dataset {ds.name}: time_dimension must name a time dimension")
                if ref.dimension.dataset != ds.name and ref.dimension.dataset not in self.reachable(ds.name):
                    raise ModelError(f"dataset {ds.name}: time_dimension {ds.time_dimension} is not "
                                     f"reachable through many-to-one joins")
        for metric in self.metrics.values():
            if metric.type == "simple":
                self.measure(metric.measure, context=f"metric {metric.name}")
                for f in metric.filters:
                    parse_dimension_ref(self, f.dimension)
            for dep in metric.dependencies():
                if dep not in self.metrics:
                    raise ModelError(f"metric {metric.name} references unknown metric {dep!r}")
        state: dict[str, int] = {}

        def visit(name: str, stack: list[str]) -> None:
            if state.get(name) == 2:
                return
            if state.get(name) == 1:
                raise ModelError("metric cycle: " + " -> ".join(stack + [name]))
            state[name] = 1
            for dep in self.metrics[name].dependencies():
                visit(dep, stack + [name])
            state[name] = 2

        for name in self.metrics:
            visit(name, [])

    # -- lookups -------------------------------------------------------------------------------

    def measure(self, ref: str | None, context: str = "") -> Measure:
        ds_name, _, m_name = (ref or "").partition(".")
        ds = self.datasets.get(ds_name)
        if ds is None or m_name not in ds.measures:
            raise ModelError(f"{context}: unknown measure {ref!r} (expected 'dataset.measure')")
        return ds.measures[m_name]

    def reachable(self, start: str) -> set[str]:
        seen, todo = set(), [start]
        while todo:
            for j in self.datasets[todo.pop()].joins:
                if j.to not in seen:
                    seen.add(j.to)
                    todo.append(j.to)
        return seen

    def dimension_refs(self) -> list[str]:
        refs = ["metric_time__" + g for g in GRAINS]
        for ds in self.datasets.values():
            for dim in ds.dimensions.values():
                refs.append(f"{ds.name}__{dim.name}")
                if dim.type == "time":
                    refs.extend(f"{ds.name}__{dim.name}__{g}" for g in GRAINS)
        return refs
