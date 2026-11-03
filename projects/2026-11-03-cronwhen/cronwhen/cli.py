"""Command line interface: ``python -m cronwhen <command> ...``."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .crontab import JobFileError, dst_report, parse_jobs
from .lint import lint
from .parser import CronSyntaxError
from .schedule import GAP_POLICIES, OVERLAP_POLICIES, Schedule


def _dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO datetime: {s!r}")


def _add_policy(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gap", choices=GAP_POLICIES, default="auto",
                   help="nonexistent wall times (spring forward)")
    p.add_argument("--overlap", choices=OVERLAP_POLICIES, default="auto",
                   help="repeated wall times (fall back)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cronwhen", description="DST-correct cron schedules")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("next", help="list upcoming fire times")
    p.add_argument("expr")
    p.add_argument("--tz", default="UTC")
    p.add_argument("--after", type=_dt, required=True,
                   help="ISO datetime; naive values are wall time in --tz")
    p.add_argument("-n", type=int, default=5)
    p.add_argument("--until", type=_dt)
    p.add_argument("--all", action="store_true",
                   help="also show non-firing occurrences (skipped, suppressed, coalesced)")
    p.add_argument("--dom-dow", choices=("or", "and"), default="or")
    p.add_argument("--json", action="store_true")
    _add_policy(p)

    p = sub.add_parser("lint", help="check expressions for likely mistakes")
    p.add_argument("exprs", nargs="+")
    p.add_argument("--tz")
    p.add_argument("--year", type=int)
    p.add_argument("--dom-dow", choices=("or", "and"), default="or")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("dst-report", help="DST-affected runs for every job in a job file")
    p.add_argument("jobfile", type=Path)
    p.add_argument("--start", type=_dt, required=True)
    p.add_argument("--end", type=_dt, required=True)
    p.add_argument("--json", action="store_true")
    _add_policy(p)
    return ap


def cmd_next(a, out) -> int:
    sched = Schedule(a.expr, a.tz, gap=a.gap, overlap=a.overlap, dom_dow=a.dom_dow)
    occs = sched.occurrences(a.after, a.until) if a.all else sched.fires(a.after, a.until)
    rows = []
    for occ in occs:
        rows.append(occ.as_dict(sched.tz))
        if a.until is None and sum(r["fires"] for r in rows) >= a.n:
            break
    if a.json:
        json.dump({"expr": a.expr, "tz": a.tz, "gap": sched.gap, "overlap": sched.overlap,
                   "occurrences": rows}, out, indent=2)
        out.write("\n")
        return 0
    out.write(f"# {a.expr}  tz={a.tz}  gap={sched.gap}  overlap={sched.overlap}\n")
    for r in rows:
        mark = "" if r["status"] == "ok" else f"  [{r['status']}"
        if r["status"] != "ok" and r["status"] != "overlap-first":
            mark += f", wall {r['local']}"
        mark += "]" if mark else ""
        prefix = "  " if r["fires"] else "- "
        out.write(f"{prefix}{r['fires_at_local']}{mark}\n")
    if not rows:
        out.write("(no fire times found)\n")
    return 0


def cmd_lint(a, out) -> int:
    worst = 0
    results = []
    for expr in a.exprs:
        findings = lint(expr, tz=a.tz, year=a.year, dom_dow=a.dom_dow)
        results.append({"expr": expr, "findings": [f.as_dict() for f in findings]})
        if any(f.level == "error" for f in findings):
            worst = max(worst, 2)
        elif any(f.level == "warning" for f in findings):
            worst = max(worst, 1)
    if a.json:
        json.dump(results, out, indent=2)
        out.write("\n")
    else:
        for r in results:
            out.write(f"{r['expr']}\n")
            if not r["findings"]:
                out.write("  ok\n")
            for f in r["findings"]:
                out.write(f"  {f['level']:<7} {f['code']:<16} {f['message']}\n")
    return worst


def cmd_dst_report(a, out) -> int:
    jobs = parse_jobs(a.jobfile.read_text())
    rows = dst_report(jobs, a.start, a.end, gap=a.gap, overlap=a.overlap)
    if a.json:
        json.dump(rows, out, indent=2)
        out.write("\n")
        return 0
    out.write(f"# DST-affected occurrences {a.start.isoformat()} .. {a.end.isoformat()}\n")
    out.write(f"{'job':<16} {'wall time':<17} {'status':<14} {'fires':<5} actual run (local)\n")
    for r in rows:
        actual = r["fires_at_local"] if r["fires"] else "-"
        out.write(f"{r['job']:<16} {r['local']:<17} {r['status']:<14} "
                  f"{'yes' if r['fires'] else 'no':<5} {actual}\n")
    unaffected = sorted({j.name for j in jobs} - {r["job"] for r in rows})
    if unaffected:
        out.write(f"# unaffected: {', '.join(unaffected)}\n")
    return 0


def main(argv: Optional[List[str]] = None, out=None) -> int:
    out = out or sys.stdout
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "next":
            return cmd_next(a, out)
        if a.cmd == "lint":
            return cmd_lint(a, out)
        return cmd_dst_report(a, out)
    except (CronSyntaxError, JobFileError, ValueError) as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    except Exception as e:  # unknown zone etc.
        if e.__class__.__name__ == "ZoneInfoNotFoundError":
            sys.stderr.write(f"error: unknown time zone: {e}\n")
            return 2
        raise
