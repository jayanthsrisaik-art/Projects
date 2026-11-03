"""Calendar matching and the fast field-skipping search on naive local time."""
from __future__ import annotations

import calendar
from bisect import bisect_left
from datetime import date, datetime, timedelta
from typing import Optional

from .parser import CronSpec


def cron_weekday(d: date) -> int:
    """0 = Sunday ... 6 = Saturday."""
    return (d.weekday() + 1) % 7


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def nearest_weekday(year: int, month: int, day: int) -> Optional[int]:
    """Day-of-month of the Mon-Fri day nearest to ``day`` without leaving the month.

    Returns None when the month has no such day (e.g. ``31W`` in April).
    """
    last = days_in_month(year, month)
    if day > last:
        return None
    wd = date(year, month, day).weekday()  # Mon=0
    if wd < 5:
        return day
    if wd == 5:  # Saturday -> Friday, unless that leaves the month
        return day - 1 if day > 1 else day + 2
    return day + 1 if day < last else day - 2  # Sunday -> Monday


def last_weekday(year: int, month: int) -> int:
    last = days_in_month(year, month)
    wd = date(year, month, last).weekday()
    return last - max(0, wd - 4)


def dom_matches(spec: CronSpec, d: date) -> bool:
    if d.day in spec.dom:
        return True
    last = days_in_month(d.year, d.month)
    if spec.dom_last and (last - d.day) in spec.dom_last:
        return True
    for n in spec.dom_weekday:
        if nearest_weekday(d.year, d.month, n) == d.day:
            return True
    if spec.dom_last_weekday and last_weekday(d.year, d.month) == d.day:
        return True
    return False


def dow_matches(spec: CronSpec, d: date) -> bool:
    wd = cron_weekday(d)
    if wd in spec.dow:
        return True
    if wd in spec.dow_last and d.day + 7 > days_in_month(d.year, d.month):
        return True
    if spec.dow_nth and (wd, (d.day - 1) // 7 + 1) in spec.dow_nth:
        return True
    return False


def day_matches(spec: CronSpec, d: date) -> bool:
    if d.month not in spec.months:
        return False
    # Vixie: a day field starting with '*' (incl. '*/2') switches to AND; its
    # set still applies, so '*/2' restricts days while plain '*' matches all.
    if spec.dom_star or spec.dow_star or spec.dom_dow == "and":
        return dom_matches(spec, d) and dow_matches(spec, d)
    return dom_matches(spec, d) or dow_matches(spec, d)


def matches(spec: CronSpec, t: datetime) -> bool:
    """Does naive local wall time ``t`` (minute precision) match the spec?"""
    return t.minute in spec.minutes and t.hour in spec.hours and day_matches(spec, t.date())


class _Sorted:
    """Sorted tuple with a 'first value >= x' lookup."""

    def __init__(self, values):
        self.v = tuple(sorted(values))

    def ceil(self, x: int) -> Optional[int]:
        i = bisect_left(self.v, x)
        return self.v[i] if i < len(self.v) else None


class LocalSearcher:
    """Finds matching naive wall-clock minutes by skipping whole fields.

    Months that cannot match are skipped wholesale, then days, then hours and
    minutes, so a search costs O(days scanned) instead of O(minutes scanned).
    """

    def __init__(self, spec: CronSpec, horizon_years: int = 400):
        self.spec = spec
        self.horizon_years = horizon_years
        self._months = _Sorted(spec.months)
        self._hours = _Sorted(spec.hours)
        self._minutes = _Sorted(spec.minutes)

    def next_at_or_after(self, t: datetime) -> Optional[datetime]:
        """Smallest matching naive minute >= t, or None within the horizon.

        A 400-year horizon covers a whole Gregorian cycle (leap years and
        weekdays repeat), so None means the expression can never fire.
        """
        spec = self.spec
        if t.second or t.microsecond:
            t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit_year = min(t.year + self.horizon_years, 9998)
        while True:
            if t.year > limit_year:
                return None
            if t.month not in spec.months:
                nm = self._months.ceil(t.month + 1)
                if nm is None:
                    if t.year + 1 > limit_year:
                        return None
                    t = datetime(t.year + 1, self._months.v[0], 1)
                else:
                    t = datetime(t.year, nm, 1)
                continue
            if not day_matches(spec, t.date()):
                t = datetime(t.year, t.month, t.day) + timedelta(days=1)
                continue
            h = self._hours.ceil(t.hour)
            if h is None:
                t = datetime(t.year, t.month, t.day) + timedelta(days=1)
                continue
            if h != t.hour:
                t = t.replace(hour=h, minute=0)
            m = self._minutes.ceil(t.minute)
            if m is None:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            return t.replace(minute=m)
