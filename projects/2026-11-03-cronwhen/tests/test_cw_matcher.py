import random
from datetime import date, datetime, timedelta

import pytest

from cronwhen import parse
from cronwhen.matcher import LocalSearcher, day_matches, last_weekday, nearest_weekday
from cronwhen.oracle import oracle_matches
from cw_helpers import random_expr


def test_nearest_weekday_rules():
    # 2026-08-01 is a Saturday: 1W cannot go back into July -> Monday 3rd
    assert nearest_weekday(2026, 8, 1) == 3
    # 2026-05-31 is a Sunday and the last day: 31W -> Friday 29th
    assert nearest_weekday(2026, 5, 31) == 29
    # 2026-11-15 is a Sunday -> Monday 16th; 2026-11-14 Saturday -> Friday 13th
    assert nearest_weekday(2026, 11, 15) == 16
    assert nearest_weekday(2026, 11, 14) == 13
    assert nearest_weekday(2026, 4, 31) is None
    assert last_weekday(2026, 1) == 30  # Jan 31 2026 is a Saturday


def days(expr, start, n_days, **kw):
    spec = parse(expr, **kw)
    return [start + timedelta(days=i) for i in range(n_days)
            if day_matches(spec, start + timedelta(days=i))]


def test_last_and_nth_weekday():
    assert days("0 0 * * 5L", date(2026, 1, 1), 90) == [
        date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 27)]
    assert days("0 0 * * MON#1", date(2026, 1, 1), 70) == [
        date(2026, 1, 5), date(2026, 2, 2), date(2026, 3, 2)]
    # 5th Saturday exists only in some months
    assert days("0 0 * * 6#5", date(2026, 1, 1), 365)[:2] == [date(2026, 1, 31), date(2026, 5, 30)]
    assert days("0 0 L-1 2 *", date(2028, 1, 1), 366) == [date(2028, 2, 28)]


def test_dom_dow_or_vs_and():
    or_days = days("0 0 13 * FRI", date(2026, 1, 1), 365)
    and_days = days("0 0 13 * FRI", date(2026, 1, 1), 365, dom_dow="and")
    assert date(2026, 2, 13) in and_days and date(2026, 3, 13) in and_days
    assert and_days == [date(2026, 2, 13), date(2026, 3, 13), date(2026, 11, 13)]
    assert len(or_days) == 52 + 12 - 3  # every Friday plus every 13th, Fri the 13th once
    # a step on dom makes it a wildcard (Vixie): AND with the weekday
    assert all(d.isoweekday() == 1 and d.day % 2 == 1
               for d in days("0 0 */2 * MON", date(2026, 1, 1), 365))


def test_searcher_skips_fields_and_detects_never():
    s = LocalSearcher(parse("0 0 29 2 *"))
    assert s.next_at_or_after(datetime(2026, 3, 1)) == datetime(2028, 2, 29)
    assert LocalSearcher(parse("0 0 30 2 *")).next_at_or_after(datetime(2026, 1, 1)) is None
    s = LocalSearcher(parse("59 23 31 12 *"))
    assert s.next_at_or_after(datetime(2026, 12, 31, 23, 59)) == datetime(2026, 12, 31, 23, 59)
    assert s.next_at_or_after(datetime(2026, 12, 31, 23, 59, 1)) == datetime(2027, 12, 31, 23, 59)


@pytest.mark.parametrize("seed", range(6))
def test_searcher_equals_minute_scan_with_independent_matcher(seed):
    """Fast field-skipping search == naive minute scan using the oracle matcher."""
    rng = random.Random(seed)
    for _ in range(25):
        expr = random_expr(rng, dst_hours=False)
        spec = parse(expr, dom_dow=rng.choice(["or", "and"]))
        searcher = LocalSearcher(spec)
        start = datetime(rng.randint(2000, 2040), rng.randint(1, 12), rng.randint(1, 28),
                         rng.randint(0, 23), rng.randint(0, 59))
        end = start + timedelta(days=4)
        fast, t = [], start
        while True:
            t = searcher.next_at_or_after(t)
            if t is None or t > end:
                break
            fast.append(t)
            t += timedelta(minutes=1)
        slow = []
        t = start
        while t <= end:
            if oracle_matches(spec, t):
                slow.append(t)
            t += timedelta(minutes=1)
        assert fast == slow, expr


@pytest.mark.parametrize("seed", range(4))
def test_day_matching_agrees_with_oracle_over_decades(seed):
    rng = random.Random(100 + seed)
    for _ in range(15):
        spec = parse(random_expr(rng), dom_dow=rng.choice(["or", "and"]))
        d = date(1995 + rng.randint(0, 40), 1, 1)
        for _ in range(3 * 366):
            want = oracle_matches(spec, datetime(d.year, d.month, d.day,
                                                 min(spec.hours), min(spec.minutes)))
            assert day_matches(spec, d) == want, (spec.expr, d)
            d += timedelta(days=1)
