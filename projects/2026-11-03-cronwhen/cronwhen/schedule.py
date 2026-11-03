"""Time-zone aware schedule: maps matching wall-clock minutes to real instants.

DST policies
------------
gap     (spring forward, wall time does not exist)
    ``shift``  fire once at the transition instant (first valid instant after the gap)
    ``skip``   do not fire
    ``auto``   ``skip`` for interval jobs, ``shift`` for fixed-time jobs (Vixie cron)
overlap (fall back, wall time happens twice)
    ``once``   fire only at the first occurrence
    ``twice``  fire at both occurrences
    ``auto``   ``twice`` for interval jobs, ``once`` for fixed-time jobs (Vixie cron)

Interval jobs are those whose minute or hour field starts with ``*``; under the
``auto`` policies they keep real elapsed-time spacing across DST changes while
fixed-time jobs run exactly once per matching day.

Fires with the same instant are coalesced (e.g. 02:30 shifted to 03:00 and a
real 03:00 match): the unshifted one fires, the other is reported as
``coalesced``.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterator, List, Optional, Tuple, Union

from zoneinfo import ZoneInfo

from .matcher import LocalSearcher
from .parser import CronSpec, parse

UTC = timezone.utc
GAP_POLICIES = ("auto", "shift", "skip")
OVERLAP_POLICIES = ("auto", "once", "twice")

# Any real UTC offset lies within +-14h; 26h of slack is used for look-ahead so
# events can be released in instant order even though candidates arrive in
# wall-clock order.
_SLACK = timedelta(hours=26)

# status values
OK = "ok"                  # unambiguous wall time
FIRST = "overlap-first"    # first of two occurrences of an ambiguous wall time
REPEAT = "repeat"          # second occurrence, fired (overlap=twice)
SUPPRESSED = "suppressed"  # second occurrence, not fired (overlap=once)
SHIFTED = "shifted"        # nonexistent wall time, fired at the transition
SKIPPED = "skipped"        # nonexistent wall time, not fired
COALESCED = "coalesced"    # would fire at an instant that already fired

_FIRING = {OK, FIRST, REPEAT, SHIFTED}


@dataclass(frozen=True)
class Occurrence:
    """One matching wall-clock minute (or one of its two DST occurrences)."""

    local: datetime          # naive wall-clock time that matched the expression
    instant: datetime        # aware UTC instant (transition instant for gaps)
    status: str

    @property
    def fires(self) -> bool:
        return self.status in _FIRING

    def as_dict(self, tz: tzinfo) -> dict:
        return {
            "local": self.local.strftime("%Y-%m-%d %H:%M"),
            "instant_utc": self.instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fires_at_local": self.instant.astimezone(tz).isoformat(),
            "status": self.status,
            "fires": self.fires,
        }


def get_tz(name: Union[str, tzinfo]) -> tzinfo:
    if isinstance(name, tzinfo):
        return name
    if name.upper() == "UTC":
        return UTC
    return ZoneInfo(name)


def resolve_wall_time(tz: tzinfo, t: datetime) -> Tuple[str, List[datetime]]:
    """Classify naive wall time ``t`` in ``tz``.

    Returns ``("ok", [u])``, ``("overlap", [first, second])`` or
    ``("gap", [transition_instant])`` with aware UTC instants.
    """
    a = t.replace(tzinfo=tz, fold=0).astimezone(UTC)
    b = t.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if a == b:
        return "ok", [a]
    lo, hi = min(a, b), max(a, b)
    if lo.astimezone(tz).replace(tzinfo=None) == t:
        return "overlap", [lo, hi]
    return "gap", [_transition_instant(tz, t, lo, hi)]


def _local(tz: tzinfo, u: datetime) -> datetime:
    return u.astimezone(tz).replace(tzinfo=None)


def _transition_instant(tz: tzinfo, t: datetime, lo: datetime, hi: datetime) -> datetime:
    """First instant whose wall time is past the nonexistent wall time ``t``.

    ``lo`` (t read with the post-transition offset) is before the jump and
    ``hi`` (t read with the pre-transition offset) is after it, so the wall
    clock passes from < t to > t somewhere in between: binary search seconds.
    """
    lo_s, hi_s = 0, int((hi - lo).total_seconds())
    while lo_s < hi_s:
        mid = (lo_s + hi_s) // 2
        if _local(tz, lo + timedelta(seconds=mid)) > t:
            hi_s = mid
        else:
            lo_s = mid + 1
    return lo + timedelta(seconds=lo_s)


def effective_policies(spec: CronSpec, gap: str, overlap: str) -> Tuple[str, str]:
    if gap not in GAP_POLICIES:
        raise ValueError(f"gap policy must be one of {GAP_POLICIES}")
    if overlap not in OVERLAP_POLICIES:
        raise ValueError(f"overlap policy must be one of {OVERLAP_POLICIES}")
    if gap == "auto":
        gap = "skip" if spec.is_interval else "shift"
    if overlap == "auto":
        overlap = "twice" if spec.is_interval else "once"
    return gap, overlap


def _aware(dt: datetime, tz: tzinfo) -> datetime:
    """Interpret a naive datetime as wall time in tz (fold=0); aware stays as is."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


class Schedule:
    """A cron expression bound to a time zone and DST policies."""

    def __init__(self, expr: Union[str, CronSpec], tz: Union[str, tzinfo] = "UTC",
                 gap: str = "auto", overlap: str = "auto", dom_dow: str = "or",
                 horizon_years: int = 400):
        self.spec = expr if isinstance(expr, CronSpec) else parse(expr, dom_dow=dom_dow)
        self.tz = get_tz(tz)
        self.tz_name = tz if isinstance(tz, str) else str(tz)
        self.gap, self.overlap = effective_policies(self.spec, gap, overlap)
        self._search = LocalSearcher(self.spec, horizon_years=horizon_years)

    # -- core ---------------------------------------------------------------
    def occurrences(self, after: datetime, until: Optional[datetime] = None
                    ) -> Iterator[Occurrence]:
        """All occurrences with instant strictly after ``after`` (and <= ``until``),
        in instant order, including non-firing ones (skipped, suppressed, coalesced).

        Naive ``after``/``until`` are read as wall time in the schedule's zone.
        """
        after_u = _aware(after, self.tz)
        until_u = _aware(until, self.tz) if until is not None else None
        heap: list = []
        seq = 0
        # Start a day early in wall time: candidates are wall-clock ordered,
        # instants may be up to ~a day away from the naive reading.
        cand = self._search.next_at_or_after(_local(self.tz, after_u) - _SLACK)
        last_fired: Optional[datetime] = None
        while True:
            if cand is not None:
                for occ in self._events_for(cand):
                    prio = 1 if occ.status in (SHIFTED, SKIPPED) else 0
                    heapq.heappush(heap, (occ.instant, prio, seq, occ))
                    seq += 1
                # every later candidate's instant is >= its wall time - 14h
                release_upto = (cand - _SLACK).replace(tzinfo=UTC)
            else:
                release_upto = None
            while heap and (release_upto is None or heap[0][0] <= release_upto):
                _, _, _, occ = heapq.heappop(heap)
                if occ.instant <= after_u:
                    continue
                if until_u is not None and occ.instant > until_u:
                    return
                if occ.fires:
                    # events leave the heap in instant order, so a duplicate
                    # instant can only repeat the most recent fire
                    if occ.instant == last_fired:
                        occ = Occurrence(occ.local, occ.instant, COALESCED)
                    else:
                        last_fired = occ.instant
                yield occ
            if cand is None:
                return
            if until_u is not None and (cand - _SLACK).replace(tzinfo=UTC) > until_u:
                cand = None  # flush what is left; nothing later can be <= until
                continue
            cand = self._search.next_at_or_after(cand + timedelta(minutes=1))

    def _events_for(self, t: datetime) -> List[Occurrence]:
        kind, instants = resolve_wall_time(self.tz, t)
        if kind == "ok":
            return [Occurrence(t, instants[0], OK)]
        if kind == "overlap":
            second = REPEAT if self.overlap == "twice" else SUPPRESSED
            return [Occurrence(t, instants[0], FIRST), Occurrence(t, instants[1], second)]
        return [Occurrence(t, instants[0], SHIFTED if self.gap == "shift" else SKIPPED)]

    # -- conveniences -------------------------------------------------------
    def fires(self, after: datetime, until: Optional[datetime] = None) -> Iterator[Occurrence]:
        return (o for o in self.occurrences(after, until) if o.fires)

    def next(self, after: datetime, n: int = 1) -> List[datetime]:
        """The next ``n`` fire instants (aware, in the schedule's zone)."""
        out: List[datetime] = []
        for occ in self.fires(after):
            out.append(occ.instant.astimezone(self.tz))
            if len(out) >= n:
                break
        return out

    def between(self, start: datetime, end: datetime) -> List[datetime]:
        return [o.instant.astimezone(self.tz) for o in self.fires(start, end)]
