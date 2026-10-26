"""Command line interface: generate | run | evaluate | lookup | export | review."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys

from goldenrec.evaluate import blocking_metrics, pairwise_metrics
from goldenrec.matcher import baseline_clusters, resolve
from goldenrec.normalize import FIELDS, normalize_record
from goldenrec.store import connect, export_golden, lookup, read_csv, run_pipeline
from goldenrec.synth import generate


def _write_csv(path: str, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def cmd_generate(args) -> int:
    records, truth = generate(args.entities, args.seed)
    _write_csv(args.out, records, ["source", "source_id", *FIELDS])
    _write_csv(args.truth, truth, ["source", "source_id", "entity_id"])
    print(f"wrote {len(records)} records for {args.entities} entities -> {args.out} (truth: {args.truth})")
    return 0


def cmd_run(args) -> int:
    rows = [row for path in args.inputs for row in read_csv(path)]
    summary = run_pipeline(args.db, rows)
    print(json.dumps(summary.as_dict(), indent=2))
    return 0


def cmd_evaluate(args) -> int:
    records = [normalize_record(r) for r in read_csv(args.input)]
    truth = read_csv(args.truth)
    result = resolve(records)
    report = {
        "records": len(records),
        "goldenrec": pairwise_metrics(result.clusters, truth),
        "baseline_exact_email_or_phone": pairwise_metrics(baseline_clusters(records), truth),
        "blocking": blocking_metrics(result.candidate_set, truth),
        "blocked_merges": result.blocked_merges,
        "review_pairs": len(result.review),
    }
    print(json.dumps(report, indent=2))
    return 0


def cmd_lookup(args) -> int:
    conn = connect(args.db)
    current = lookup(conn, args.golden_id)
    if current is None:
        print(f"unknown golden id {args.golden_id}", file=sys.stderr)
        return 1
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM golden_records WHERE golden_id = ?", (current,)).fetchone()
    members = conn.execute("SELECT source, source_id FROM crosswalk WHERE golden_id = ? ORDER BY 1, 2",
                           (current,)).fetchall()
    print(json.dumps({"requested": args.golden_id, "current": current, "record": dict(row),
                      "members": [f"{s}:{i}" for s, i in members]}, indent=2))
    return 0


def cmd_export(args) -> int:
    n = export_golden(connect(args.db), args.out)
    print(f"wrote {n} golden records -> {args.out}")
    return 0


def cmd_review(args) -> int:
    conn = connect(args.db)
    rows = conn.execute(
        "SELECT a_source, a_id, b_source, b_id, score, reasons FROM review_queue "
        "WHERE run_id = (SELECT MAX(run_id) FROM runs) ORDER BY score DESC LIMIT ?", (args.limit,)).fetchall()
    for a_s, a_i, b_s, b_i, score, reasons in rows:
        print(f"{score:5.1f}  {a_s}:{a_i} <-> {b_s}:{b_i}  [{reasons}]")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="goldenrec", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic source records + ground truth")
    g.add_argument("--entities", type=int, default=300)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--out", default="customers.csv")
    g.add_argument("--truth", default="truth.csv")
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("run", help="load CSVs into the store and rebuild golden records")
    r.add_argument("--db", default="goldenrec.db")
    r.add_argument("inputs", nargs="+")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("evaluate", help="pairwise P/R/F1 vs ground truth, with baseline")
    e.add_argument("input")
    e.add_argument("truth")
    e.set_defaults(func=cmd_evaluate)

    lk = sub.add_parser("lookup", help="resolve a (possibly retired) golden id")
    lk.add_argument("golden_id")
    lk.add_argument("--db", default="goldenrec.db")
    lk.set_defaults(func=cmd_lookup)

    x = sub.add_parser("export", help="write golden records to CSV")
    x.add_argument("--db", default="goldenrec.db")
    x.add_argument("--out", default="golden.csv")
    x.set_defaults(func=cmd_export)

    v = sub.add_parser("review", help="show possible matches for clerical review")
    v.add_argument("--db", default="goldenrec.db")
    v.add_argument("--limit", type=int, default=20)
    v.set_defaults(func=cmd_review)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
