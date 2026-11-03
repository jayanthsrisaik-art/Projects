"""Brute-force reference implementation used to verify :class:`Schedule`.

It walks the *UTC* timeline one minute at a time and decides, for each real
minute, whether the job fires - the opposite direction from the fast engine,
which walks wall-clock time and maps it to instants.  Day matching is also
re-implemented here with different formulations so that a shared bug in the
fast matcher would show up as a disagreement.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import List

from .parser import CronSpec
from .schedule import UTC, effective_policies


def _month_len(y: int, m: int) -> int:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - date(y, m, 1)).days


def _is_workday(d: date) -> bool:
    return d.isoweekday() <= 5


def _oracle_dom(spec: CronSpec, d: date) -> bool:
    n = _month_len(d.year, d.month)
    if d.day in spec.dom:
        return True
    for off in spec.dom_last:
        if d.day == n - off:
            return True
    if spec.dom_last_weekday:
        # last day of the month that is a workday: scan backwards
        k = n
        while not _is_workday(date(d.year, d.month, k)):
            k -= 1
        if k == d.day:
            return True
    if spec.dom_weekday and _is_workday(d):
        # d is the nW day iff it is the closest in-month workday to n
        # (ties cannot happen: weekend runs are 2 long)
        for target in spec.dom_weekday:
            if target > n:
                continue
            best = min((k for k in range(1, n + 1) if _is_workday(date(d.year, d.month, k))),
                       key=lambda k: (abs(k - target), k))
            if best == d.day:
                return True
    return False


def _oracle_dow(spec: CronSpec, d: date) -> bool:
    wd = d.isoweekday() % 7  # Sunday -> 0
    if wd in spec.dow:
        return True
    same_wd_days = [k for k in range(1, _month_len(d.year, d.month) + 1)
                    if date(d.year, d.month, k).isoweekday() % 7 == wd]
    if wd in spec.dow_last and same_wd_days[-1] == d.day:
        return True
    for w, k in spec.dow_nth:
        if w == wd and len(same_wd_days) >= k and same_wd_days[k - 1] == d.day:
            return True
    return False


def oracle_matches(spec: CronSpec, t: datetime) -> bool:
    if t.minute not in spec.minutes or t.hour not in spec.hours or t.month not in spec.months:
        return False
    d = t.date()
    dom_ok, dow_ok = _oracle_dom(spec, d), _oracle_dow(spec, d)
    either_wild = spec.dom_star or spec.dow_star
    return (dom_ok and dow_ok) if (either_wild or spec.dom_dow == "and") else (dom_ok or dow_ok)


def brute_force_fires(spec: CronSpec, tz: tzinfo, after: datetime, until: datetime,
                      gap: str = "auto", overlap: str = "auto") -> List[datetime]:
    """Fire instants (aware UTC) in (after, until] by scanning every real minute."""
    gap, overlap = effective_policies(spec, gap, overlap)
    after_u = after.astimezone(UTC)
    until_u = until.astimezone(UTC)
    u = after_u.replace(second=0, microsecond=0) + timedelta(minutes=1)
    prev_local = (u - timedelta(minutes=1)).astimezone(tz)
    out: List[datetime] = []
    step = timedelta(minutes=1)
    while u <= until_u:
        loc = u.astimezone(tz)
        wall = loc.replace(tzinfo=None)
        fire = False
        if oracle_matches(spec, wall):
            # fold == 1 marks the second pass through a repeated wall time
            fire = loc.fold == 0 or overlap == "twice"
        expected = prev_local.replace(tzinfo=None) + step
        if not fire and gap == "shift" and wall > expected:
            # the clock jumped forward: wall minutes [expected, wall) never happened
            k = expected
            while k < wall:
                if oracle_matches(spec, k):
                    fire = True
                    break
                k += step
        if fire:
            out.append(u)
        prev_local = loc
        u += step
    return out
