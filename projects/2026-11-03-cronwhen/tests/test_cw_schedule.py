import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cronwhen import Schedule, parse, resolve_wall_time
from cronwhen.oracle import brute_force_fires
from cw_helpers import random_expr, transitions

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def fires(expr, tz, start, end, **kw):
    s = Schedule(expr, tz, **kw)
    return [o.instant.astimezone(s.tz).isoformat() for o in s.fires(start, end)]


def statuses(expr, tz, start, end, **kw):
    s = Schedule(expr, tz, **kw)
    return [(o.local.strftime("%H:%M"), o.status) for o in s.occurrences(start, end)]


def test_resolve_wall_time_classifies_gap_and_overlap():
    kind, [u] = resolve_wall_time(NY, datetime(2026, 3, 8, 2, 30))
    assert kind == "gap" and u == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)  # 03:00 EDT
    kind, (a, b) = resolve_wall_time(NY, datetime(2026, 11, 1, 1, 30))
    assert kind == "overlap" and b - a == timedelta(hours=1)
    assert resolve_wall_time(NY, datetime(2026, 7, 1, 12, 0))[0] == "ok"


def test_fixed_time_job_shifted_into_gap_and_run_once_in_overlap():
    assert fires("30 2 * * *", "America/New_York", datetime(2026, 3, 7, 12), datetime(2026, 3, 9, 12)) == [
        "2026-03-08T03:00:00-04:00", "2026-03-09T02:30:00-04:00"]
    assert fires("30 1 * * *", "America/New_York", datetime(2026, 10, 31, 12), datetime(2026, 11, 1, 12)) == [
        "2026-11-01T01:30:00-04:00"]


def test_explicit_policies_override_auto():
    assert fires("30 2 * * *", "America/New_York", datetime(2026, 3, 8), datetime(2026, 3, 8, 12),
                 gap="skip") == []
    assert fires("30 1 * * *", "America/New_York", datetime(2026, 11, 1), datetime(2026, 11, 1, 12),
                 overlap="twice") == ["2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00"]


def test_interval_job_keeps_real_time_spacing():
    s = Schedule("*/30 * * * *", "America/New_York")
    spring = [o.instant for o in s.fires(datetime(2026, 3, 8, 0), datetime(2026, 3, 8, 5))]
    fall = [o.instant for o in s.fires(datetime(2026, 11, 1, 0), datetime(2026, 11, 1, 4))]
    for seq in (spring, fall):
        assert {b - a for a, b in zip(seq, seq[1:])} == {timedelta(minutes=30)}
    assert len(fall) == 10  # 4 wall hours that contain 5 real hours


def test_shift_coalesces_with_real_match_at_transition():
    # 02:00, 02:30 shifted to 03:00, which is also a real match
    st = statuses("0,30 2-3 * * *", "America/New_York", datetime(2026, 3, 8), datetime(2026, 3, 8, 4),
                  gap="shift")
    assert st == [("03:00", "ok"), ("02:00", "coalesced"), ("02:30", "coalesced"), ("03:30", "ok")]


def test_half_hour_dst_lord_howe():
    # Lord Howe Island moves 02:00 -> 02:30 in October
    tz = "Australia/Lord_Howe"
    assert fires("15 2 * * *", tz, datetime(2026, 10, 3, 12), datetime(2026, 10, 4, 12)) == [
        "2026-10-04T02:30:00+11:00"]
    assert fires("45 2 * * *", tz, datetime(2026, 10, 3, 12), datetime(2026, 10, 4, 12)) == [
        "2026-10-04T02:45:00+11:00"]


def test_samoa_skipped_whole_day():
    # Pacific/Apia jumped from 2011-12-29 23:59 to 2011-12-31 00:00
    got = fires("0 9 * * *", "Pacific/Apia", datetime(2011, 12, 29), datetime(2011, 12, 31, 12))
    assert got == ["2011-12-29T09:00:00-10:00", "2011-12-31T00:00:00+14:00",
                   "2011-12-31T09:00:00+14:00"]
    assert fires("0 9 * * *", "Pacific/Apia", datetime(2011, 12, 29), datetime(2011, 12, 31, 12),
                 gap="skip") == ["2011-12-29T09:00:00-10:00", "2011-12-31T09:00:00+14:00"]


def test_midnight_transition_santiago():
    # Chile springs forward at 24:00 -> 01:00 (first Sunday of September 2026)
    got = fires("30 0 * * *", "America/Santiago", datetime(2026, 9, 5, 12), datetime(2026, 9, 6, 12))
    assert got == ["2026-09-06T01:00:00-03:00"]


def test_after_is_exclusive_and_aware_inputs_accepted():
    s = Schedule("0 * * * *", "Europe/Berlin")
    at = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert s.next(at, 2) == [at + timedelta(hours=1), at + timedelta(hours=2)]
    assert s.next(datetime(2026, 6, 1, 8, 0, tzinfo=UTC))[0].isoformat() == "2026-06-01T11:00:00+02:00"


def test_never_firing_expression_terminates():
    assert Schedule("0 0 30 2 *", "UTC").next(datetime(2026, 1, 1)) == []


def test_until_limits_far_future_scan():
    s = Schedule("0 0 29 2 *", "UTC")
    assert s.between(datetime(2026, 1, 1), datetime(2027, 1, 1)) == []
    assert s.next(datetime(2026, 1, 1))[0].year == 2028


ZONES = ["America/New_York", "Europe/London", "Europe/Dublin", "Australia/Lord_Howe",
         "Pacific/Chatham", "America/Santiago", "America/Havana", "Australia/Sydney",
         "Africa/Casablanca", "Asia/Tehran", "Pacific/Apia", "America/St_Johns"]


@pytest.mark.parametrize("zone", ZONES)
def test_engine_matches_utc_minute_oracle_around_transitions(zone):
    tz = ZoneInfo(zone)
    rng = random.Random(zone)
    trans = transitions(tz, datetime(2008, 1, 1), datetime(2031, 1, 1))
    assert trans, zone
    picks = rng.sample(trans, min(4, len(trans)))
    if zone == "Pacific/Apia":
        picks.append(datetime(2011, 12, 30, 10, 0, tzinfo=UTC))  # the skipped day
    checked = 0
    for t in picks:
        start = t - timedelta(hours=rng.randint(20, 40))
        end = t + timedelta(hours=rng.randint(20, 40))
        for _ in range(6):
            expr = random_expr(rng)
            spec = parse(expr)
            for gap, overlap in [("auto", "auto"), ("shift", "twice"), ("skip", "once")]:
                fast = [o.instant for o in Schedule(spec, tz, gap=gap, overlap=overlap)
                        .fires(start, end)]
                slow = brute_force_fires(spec, tz, start, end, gap=gap, overlap=overlap)
                assert fast == slow, (zone, expr, gap, overlap, t)
                checked += 1
    assert checked >= 72


def test_fires_are_strictly_increasing_over_a_year():
    for zone in ("Europe/London", "Australia/Lord_Howe"):
        s = Schedule("*/7 0-3 * * *", zone)
        seq = [o.instant for o in s.fires(datetime(2026, 1, 1), datetime(2027, 1, 1))]
        assert all(a < b for a, b in zip(seq, seq[1:]))


def test_naive_baseline_failure_modes_vs_engine():
    from cronwhen.baseline import naive_fires
    start = datetime(2026, 3, 7, tzinfo=NY)
    end = datetime(2026, 3, 9, tzinfo=NY)
    spec = parse("30 2 * * *")
    naive = [u.astimezone(NY).isoformat() for u in naive_fires(spec, NY, start, end)]
    assert "2026-03-08T03:30:00-04:00" in naive  # nonexistent 02:30 lands an hour late
    spec = parse("*/15 * * * *")
    s, e = datetime(2026, 11, 1, tzinfo=NY), datetime(2026, 11, 1, 4, tzinfo=NY)
    naive = naive_fires(spec, NY, s, e)
    engine = [o.instant for o in Schedule(spec, NY).fires(s, e)]
    assert len(engine) - len(naive) == 4  # the repeated hour is never visited
    s, e = datetime(2026, 3, 8, tzinfo=NY), datetime(2026, 3, 8, 5, tzinfo=NY)
    naive = naive_fires(spec, NY, s, e)
    assert len(naive) - len(set(naive)) == 4  # 02:xx collide with 03:xx
