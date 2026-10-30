from datetime import date, timedelta

import pytest
from conftest import NOW, utc

from slotwise.availability import (
    ConfigError, compute_slots, event_type_from_dict, iso_z, parse_instant, resolve_local, windows_for_date,
)


def et(**kw):
    base = {"slug": "x", "host_tz": "America/New_York", "duration_min": 30, "weekly": {}}
    base.update(kw)
    return event_type_from_dict(base)


def slots(e, a, b, now=None, busy=(), counts=None):
    now = now or a - timedelta(days=2)
    return [iso_z(s) for s in compute_slots(e, range_start=a, range_end=b, now=now, busy=list(busy), bookings_per_day=counts)]


def test_weekly_windows_and_lunch_gap(ets):
    got = slots(ets["intro-call"], utc(2026, 11, 2), utc(2026, 11, 3), now=NOW)
    # Monday 09-12 and 13-17 EST (UTC-5): 6 + 8 half-hour slots, none in lunch.
    assert len(got) == 14
    assert got[0] == "2026-11-02T14:00:00Z" and got[-1] == "2026-11-02T21:30:00Z"
    assert "2026-11-02T17:00:00Z" not in got  # 12:00 local


def test_fall_back_day_has_extra_real_hour(ets):
    e = ets["night-support"]
    sunday = slots(e, utc(2026, 11, 1), utc(2026, 11, 2), now=NOW)
    normal = slots(e, utc(2026, 11, 8), utc(2026, 11, 9), now=NOW)
    assert sunday == ["2026-11-01T04:00:00Z", "2026-11-01T05:00:00Z", "2026-11-01T06:00:00Z",
                      "2026-11-01T07:00:00Z", "2026-11-01T08:00:00Z"]
    assert len(normal) == 4


def test_spring_forward_day_loses_an_hour():
    e = et(duration_min=60, weekly={"sun": [["00:00", "04:00"]]})
    got = slots(e, utc(2026, 3, 8), utc(2026, 3, 9))
    assert got == ["2026-03-08T05:00:00Z", "2026-03-08T06:00:00Z", "2026-03-08T07:00:00Z"]


def test_window_starting_in_gap_snaps_to_transition():
    e = et(weekly={"sun": [["02:30", "04:00"]]})
    assert slots(e, utc(2026, 3, 8), utc(2026, 3, 9)) == ["2026-03-08T07:00:00Z", "2026-03-08T07:30:00Z"]


def test_resolve_local_ambiguous_start_early_end_late():
    from zoneinfo import ZoneInfo
    from datetime import time
    z = ZoneInfo("America/New_York")
    assert iso_z(resolve_local(date(2026, 11, 1), time(1, 30), z, is_end=False)) == "2026-11-01T05:30:00Z"
    assert iso_z(resolve_local(date(2026, 11, 1), time(1, 30), z, is_end=True)) == "2026-11-01T06:30:00Z"
    # 24:00 is next day's midnight
    assert iso_z(resolve_local(date(2026, 11, 1), None, z, is_end=True)) == "2026-11-02T05:00:00Z"


def test_europe_london_moves_independently_of_us(ets):
    # London fell back 2026-10-25 while New York had not: Tue 27 Oct 15:00 GMT = 15:00Z.
    got = slots(ets["office-hours"], utc(2026, 10, 27), utc(2026, 10, 28), now=utc(2026, 10, 26))
    assert got[0] == "2026-10-27T15:00:00Z" and len(got) == 6


def test_min_notice_and_horizon(ets):
    e = ets["intro-call"]  # 240 min notice, 30 days ahead
    # now = Fri 08:00 NY; +4h notice = 12:00, when Friday's only window closes.
    assert slots(e, utc(2026, 10, 30), utc(2026, 10, 31), now=NOW) == []
    early = utc(2026, 10, 30, 9)  # 05:00 NY -> earliest 09:00
    assert slots(e, utc(2026, 10, 30), utc(2026, 10, 31), now=early)[0] == "2026-10-30T13:00:00Z"
    # 30-day horizon from Fri 2026-10-30 ends on host-local Sun 2026-11-29.
    assert slots(e, utc(2026, 11, 27), utc(2026, 11, 28), now=NOW)  # Fri 27th: open
    assert slots(e, utc(2026, 11, 30), utc(2026, 12, 2), now=NOW) == []


def test_busy_blocks_respect_buffers(ets):
    e = ets["intro-call"]  # 15 min after-buffer, 30-min step
    busy = [(utc(2026, 11, 2, 15, 0), utc(2026, 11, 2, 15, 45))]  # booking 10:00-10:30 + 15 buffer
    got = slots(e, utc(2026, 11, 2), utc(2026, 11, 3), now=NOW, busy=busy)
    # 09:30 slot would block until 10:15 -> overlaps; 10:00 overlaps; 10:30 overlaps buffer.
    for t in ("14:30", "15:00", "15:30"):
        assert f"2026-11-02T{t}:00Z" not in got
    assert "2026-11-02T14:00:00Z" in got and "2026-11-02T16:00:00Z" in got


def test_daily_cap_closes_day(ets):
    got = slots(ets["intro-call"], utc(2026, 11, 2), utc(2026, 11, 4), now=NOW, counts={"2026-11-02": 4})
    assert got and all(s.startswith("2026-11-03") for s in got)


def test_overrides_day_off_and_special_hours(ets):
    e = ets["intro-call"]
    assert slots(e, utc(2026, 11, 26), utc(2026, 11, 27), now=NOW) == []  # Thanksgiving
    sat = slots(e, utc(2026, 11, 7), utc(2026, 11, 8), now=NOW)  # Saturday special 10-12 EST
    assert sat == ["2026-11-07T15:00:00Z", "2026-11-07T15:30:00Z", "2026-11-07T16:00:00Z", "2026-11-07T16:30:00Z"]


def test_window_until_midnight_and_step_smaller_than_duration():
    e = et(duration_min=60, step_min=15, weekly={"fri": [["22:00", "24:00"]]})
    got = slots(e, utc(2026, 1, 1), utc(2026, 1, 10))
    # Fri 2026-01-02 22:00..23:00 EST starts every 15 min -> 5 slots
    assert got == ["2026-01-03T03:00:00Z", "2026-01-03T03:15:00Z", "2026-01-03T03:30:00Z",
                   "2026-01-03T03:45:00Z", "2026-01-03T04:00:00Z"]


def test_range_filter_is_half_open():
    e = et(weekly={"fri": [["09:00", "10:00"]]})
    got = slots(e, utc(2026, 1, 2, 14, 0), utc(2026, 1, 2, 14, 30))
    assert got == ["2026-01-02T14:00:00Z"]


def test_windows_for_date_in_other_zone():
    e = et(host_tz="Asia/Kolkata", weekly={"mon": [["09:00", "09:30"]]})
    assert windows_for_date(e, date(2026, 11, 2)) == [(utc(2026, 11, 2, 3, 30), utc(2026, 11, 2, 4, 0))]


@pytest.mark.parametrize("bad", [
    {"host_tz": "Mars/Olympus"},
    {"weekly": {"funday": [["09:00", "10:00"]]}},
    {"weekly": {"mon": [["10:00", "09:00"]]}},
    {"weekly": {"mon": [["25:00", "26:00"]]}},
    {"duration_min": 0},
    {"buffer_after_min": -5},
])
def test_config_validation(bad):
    with pytest.raises(ConfigError):
        et(**bad)


def test_naive_datetimes_rejected():
    e = et(weekly={"mon": [["09:00", "10:00"]]})
    with pytest.raises(ValueError):
        compute_slots(e, range_start=utc(2026, 1, 1).replace(tzinfo=None), range_end=utc(2026, 1, 2), now=utc(2026, 1, 1), busy=[])
    with pytest.raises(ValueError):
        parse_instant("2026-01-01T10:00:00")
    assert iso_z(parse_instant("2026-01-01T10:00:00+05:30")) == "2026-01-01T04:30:00Z"


def test_brute_force_oracle_over_a_year_of_dst():
    """Cross-check slot generation against a minute-by-minute oracle that only
    uses zoneinfo's UTC->local conversion (never local->UTC)."""
    from zoneinfo import ZoneInfo
    for tz in ("America/New_York", "Europe/London", "Australia/Lord_Howe"):
        e = et(host_tz=tz, duration_min=30, weekly={d: [["00:00", "03:30"]] for d in ("sun", "mon")})
        z = ZoneInfo(tz)
        for start in (utc(2026, 3, 7), utc(2026, 3, 28), utc(2026, 4, 4), utc(2026, 10, 3), utc(2026, 10, 24), utc(2026, 10, 31)):
            end = start + timedelta(days=3)
            got = set(slots(e, start, end))
            # Oracle: an instant is inside a window iff its local wall time on a
            # Sun/Mon is in [00:00, 03:30) (or it is the first instant after a
            # skipped 00:00..). Slots are 30-min steps from each window's first instant.
            t, inside_prev, cur = start - timedelta(days=1), False, None
            expected = set()
            while t < end + timedelta(days=1):
                lt = t.astimezone(z)
                inside = lt.weekday() in (6, 0) and (lt.hour, lt.minute) < (3, 30)
                if inside and not inside_prev:
                    cur = t  # window opens
                if inside:
                    last = t
                if not inside and inside_prev:
                    s = cur
                    while s + timedelta(minutes=30) <= last + timedelta(minutes=1):
                        if start <= s < end:
                            expected.add(iso_z(s))
                        s += timedelta(minutes=30)
                inside_prev = inside
                t += timedelta(minutes=1)
            assert got == expected, (tz, start)
