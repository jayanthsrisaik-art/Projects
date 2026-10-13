"""python -m settlement.cli generate <dir> | run <dir> [--db file]"""
import argparse
import json
import sqlite3
from pathlib import Path

from . import analytics, generate, warehouse


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate"); g.add_argument("dir"); g.add_argument("--seed", type=int, default=42)
    r = sub.add_parser("run"); r.add_argument("dir"); r.add_argument("--db", default=":memory:")
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        generate.generate(Path(a.dir), a.seed)
        return
    conn = connect(a.db)
    summary = warehouse.run_pipeline(conn, Path(a.dir))
    as_of = "2026-09-03"
    report = {
        "files_loaded": len(summary["loaded"]), "files_quarantined": summary["quarantined"],
        "rejected_rows": conn.execute("SELECT COUNT(*) FROM stg_rejects").fetchone()[0],
        "audit": summary["audit"], "fact_rows": summary["facts_loaded"],
        "notifications": [r[0] for r in conn.execute("SELECT message FROM notification_log")],
        "delinquency": analytics.delinquency_by_product(conn, as_of),
        "penetration": analytics.product_penetration(conn, as_of),
        "deposit_velocity": analytics.deposit_velocity(conn),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
