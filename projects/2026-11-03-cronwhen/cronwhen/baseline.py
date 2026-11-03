"""The naive approach many hand-rolled schedulers take, kept as a baseline.

It steps an *aware* local datetime forward with ``timedelta`` (Python's
wall-clock arithmetic, so it never sees fold=1) and fires whenever the wall time
matches.  Nonexistent wall times are converted with fold=0, which lands them an
hour late, and the repeated hour of a fall-back night is never visited.
"""
from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import List

from .matcher import matches
from .parser import CronSpec
from .schedule import UTC


def naive_fires(spec: CronSpec, tz: tzinfo, after: datetime, until: datetime) -> List[datetime]:
    t = after.astimezone(tz).replace(second=0, microsecond=0, fold=0) + timedelta(minutes=1)
    end_wall = until.astimezone(tz).replace(tzinfo=None)
    out: List[datetime] = []
    while t.replace(tzinfo=None) <= end_wall:
        if matches(spec, t.replace(tzinfo=None)):
            u = t.astimezone(UTC)
            if after.astimezone(UTC) < u <= until.astimezone(UTC):
                out.append(u)
        t = t + timedelta(minutes=1)  # wall-clock arithmetic
    return out
