"""A small job-file format and the DST impact report.

Each non-blank, non-comment line is ``<name> <time zone> <cron expression>``::

    nightly-backup  America/New_York  30 2 * * *
    metrics-rollup  Europe/London     */15 * * * *
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

from .parser import CronSyntaxError, parse
from .schedule import Schedule, get_tz


@dataclass(frozen=True)
class Job:
    name: str
    tz: str
    expr: str
    line: int


class JobFileError(ValueError):
    pass


def parse_jobs(text: str) -> List[Job]:
    jobs: List[Job] = []
    seen = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        # only whole-line comments: '#' is legal inside expressions (FRI#3)
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            raise JobFileError(f"line {i}: expected '<name> <tz> <expr>'")
        name, tz, expr = parts
        if name in seen:
            raise JobFileError(f"line {i}: duplicate job name {name!r}")
        seen.add(name)
        try:
            get_tz(tz)
        except Exception as e:  # ZoneInfoNotFoundError / ValueError
            raise JobFileError(f"line {i}: unknown time zone {tz!r}") from e
        try:
            parse(expr)
        except CronSyntaxError as e:
            raise JobFileError(f"line {i}: {e}") from e
        jobs.append(Job(name, tz, expr, i))
    return jobs


def dst_report(jobs: List[Job], start: datetime, end: datetime,
               gap: str = "auto", overlap: str = "auto") -> List[Dict]:
    """Every DST-affected occurrence of every job between start and end (wall
    time in each job's own zone)."""
    rows: List[Dict] = []
    for job in jobs:
        sched = Schedule(job.expr, job.tz, gap=gap, overlap=overlap)
        for occ in sched.occurrences(start, end):
            if occ.status == "ok":
                continue
            row = {"job": job.name, "tz": job.tz, "expr": job.expr,
                   "policy": f"gap={sched.gap},overlap={sched.overlap}"}
            row.update(occ.as_dict(sched.tz))
            rows.append(row)
    return rows
