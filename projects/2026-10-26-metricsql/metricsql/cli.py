"""Command line interface: demo-db | list | query | run."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys

from metricsql.compiler import Query, QueryError, compile_query
from metricsql.model import ModelError, SemanticModel
from metricsql.naive import compile_naive
from metricsql.synth import generate

WHERE_RE = re.compile(r"^\s*([a-z0-9_]+)\s*(!=|<=|>=|=|<|>|\bnot in\b|\bin\b|\bis not null\b|\bis null\b)\s*(.*)$",
                      re.IGNORECASE)


def _value(text: str):
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def parse_where(text: str) -> dict:
    """'orders__channel in web|app' / 'customers__region = north' / 'customers__region is null'."""
    m = WHERE_RE.match(text)
    if not m:
        raise QueryError(f"cannot parse --where {text!r}; e.g. 'orders__status = completed'")
    dim, op, rest = m.group(1), m.group(2).lower(), m.group(3).strip()
    if op in ("is null", "is not null"):
        if rest:
            raise QueryError(f"{op!r} takes no value: {text!r}")
        return {"dimension": dim, "op": op}
    if op in ("in", "not in"):
        return {"dimension": dim, "op": op, "value": [_value(v.strip()) for v in rest.split("|")]}
    return {"dimension": dim, "op": op, "value": _value(rest)}


def _fmt(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.2f}" if abs(value) >= 1 or value == 0 else f"{value:.4f}"
    return str(value)


def _print(columns: list[str], rows: list[tuple], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps([dict(zip(columns, r)) for r in rows], indent=2))
    elif fmt == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
    else:
        cells = [[_fmt(v) for v in r] for r in rows]
        widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
        print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
        print("  ".join("-" * w for w in widths))
        for r in cells:
            print("  ".join(v.rjust(w) if _is_num(v) else v.ljust(w) for v, w in zip(r, widths)))
        print(f"({len(rows)} rows)")


def _is_num(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _execute(args, query: Query) -> int:
    model = SemanticModel.load(args.model)
    compiled = (compile_naive if args.naive else compile_query)(model, query)
    if args.sql or args.explain:
        if args.explain:
            print("\n".join("-- " + line for line in compiled.plan))
        print(compiled.sql + ";")
        if compiled.params:
            print("-- params:", json.dumps(compiled.params))
        if not args.db:
            return 0
    if not args.db:
        raise QueryError("--db is required unless --sql/--explain is given")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(compiled.sql, compiled.params).fetchall()
    _print(compiled.columns, rows, args.format)
    return 0


def cmd_query(args) -> int:
    raw = {
        "metrics": [m for m in args.metrics.split(",") if m],
        "group_by": [g for g in (args.by or "").split(",") if g],
        "where": [parse_where(w) for w in args.where],
        "order_by": [o for o in (args.order or "").split(",") if o],
        "limit": args.limit,
    }
    return _execute(args, Query.from_dict(raw))


def cmd_run(args) -> int:
    with open(args.query_file, encoding="utf-8") as fh:
        return _execute(args, Query.from_dict(json.load(fh)))


def cmd_list(args) -> int:
    model = SemanticModel.load(args.model)
    print("metrics:")
    for m in model.metrics.values():
        detail = {"simple": m.measure, "ratio": f"{m.numerator} / {m.denominator}", "derived": m.expr}[m.type]
        flt = "".join(f" [{f.dimension} {f.op} {f.value}]" for f in m.filters)
        print(f"  {m.name:<18} {m.type:<8} {detail}{flt}" + (f"  -- {m.description}" if m.description else ""))
    print("dimensions:")
    for ref in model.dimension_refs():
        print(f"  {ref}")
    return 0


def cmd_demo_db(args) -> int:
    conn = sqlite3.connect(args.out)
    if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
        print(f"{args.out} is not empty; refusing to overwrite", file=sys.stderr)
        return 1
    counts = generate(conn, customers=args.customers, orders=args.orders, seed=args.seed)
    print(json.dumps(counts))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="metricsql", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo-db", help="create the synthetic shop database")
    d.add_argument("--out", required=True)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--customers", type=int, default=200)
    d.add_argument("--orders", type=int, default=3000)
    d.set_defaults(func=cmd_demo_db)

    ls = sub.add_parser("list", help="list metrics and dimension references")
    ls.add_argument("--model", required=True)
    ls.set_defaults(func=cmd_list)

    def common(sp):
        sp.add_argument("--model", required=True)
        sp.add_argument("--db")
        sp.add_argument("--sql", action="store_true", help="print the compiled SQL")
        sp.add_argument("--explain", action="store_true", help="print the plan and SQL")
        sp.add_argument("--naive", action="store_true", help="use the join-everything baseline (wrong on purpose)")
        sp.add_argument("--format", choices=["table", "csv", "json"], default="table")

    q = sub.add_parser("query", help="query metrics from the command line")
    common(q)
    q.add_argument("--metrics", required=True, help="comma-separated metric names")
    q.add_argument("--by", help="comma-separated dimension references")
    q.add_argument("--where", action="append", default=[], help="e.g. 'orders__channel in web|app'")
    q.add_argument("--order", help="comma-separated; prefix - for descending (write --order=-revenue)")
    q.add_argument("--limit", type=int)
    q.set_defaults(func=cmd_query)

    r = sub.add_parser("run", help="run a query JSON file")
    common(r)
    r.add_argument("query_file")
    r.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ModelError, QueryError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
