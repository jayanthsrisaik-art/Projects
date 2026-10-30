from datetime import date, time
from zoneinfo import ZoneInfo

import pytest

from bookgrid.recurrence import Rule, RuleError, expand_dates, localize, parse_rrule


def test_parse_and_roundtrip():
    rule = parse_rrule("RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=WE,MO;COUNT=6")
    assert rule == Rule("WEEKLY", 2, (0, 2), 6, None)
    assert rule.to_string() == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=6"
    assert parse_rrule(rule.to_string()) == rule


@pytest.mark.parametrize("text, msg", [
    ("FREQ=MONTHLY;COUNT=3", "FREQ"),
    ("FREQ=WEEKLY", "bounded"),
    ("FREQ=DAILY;COUNT=2;UNTIL=20261231", "mutually exclusive"),
    ("FREQ=DAILY;BYDAY=MO;COUNT=2", "only supported"),
    ("FREQ=WEEKLY;BYDAY=XX;COUNT=2", "BYDAY"),
    ("FREQ=WEEKLY;COUNT=0", "COUNT"),
    ("FREQ=WEEKLY;COUNT=2;BYMONTH=1", "unsupported"),
    ("FREQ=WEEKLY;INTERVAL=0;COUNT=2", "INTERVAL"),
    ("FREQ=DAILY;FREQ=DAILY;COUNT=1", "duplicate"),
])
def test_rejects_bad_rules(text, msg):
    with pytest.raises(RuleError, match=msg):
        parse_rrule(text)


def test_weekly_byday_interval_count():
    # Start Wednesday 2026-11-04, every other week on Mon+Wed: the Monday of the
    # first week is before the start, so it is skipped (dateutil semantics).
    rule = parse_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=5")
    assert expand_dates(date(2026, 11, 4), rule) == [
        date(2026, 11, 4), date(2026, 11, 16), date(2026, 11, 18),
        date(2026, 11, 30), date(2026, 12, 2),
    ]


def test_weekly_defaults_to_start_weekday_and_until_is_inclusive():
    rule = parse_rrule("FREQ=WEEKLY;UNTIL=20261123")
    assert expand_dates(date(2026, 11, 2), rule) == [
        date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 23)]


def test_daily_interval():
    rule = parse_rrule("FREQ=DAILY;INTERVAL=3;COUNT=3")
    assert expand_dates(date(2026, 12, 30), rule) == [
        date(2026, 12, 30), date(2027, 1, 2), date(2027, 1, 5)]


def test_unbounded_until_is_capped():
    with pytest.raises(RuleError, match="more than"):
        expand_dates(date(2026, 1, 1), parse_rrule("FREQ=DAILY;UNTIL=20301231"))


def test_wall_time_kept_across_dst():
    tz = ZoneInfo("America/New_York")
    before = localize(date(2026, 10, 29), time(11), tz)
    after = localize(date(2026, 11, 2), time(11), tz)
    assert before.utc.hour == 15 and after.utc.hour == 16
    assert before.note is None and after.note is None


def test_spring_forward_gap_shifts_forward():
    # US clocks jump 02:00 -> 03:00 on 2027-03-14.
    inst = localize(date(2027, 3, 14), time(2, 30), ZoneInfo("America/New_York"))
    assert inst.utc.isoformat() == "2027-03-14T07:30:00+00:00"  # = 03:30 EDT
    assert "does not exist" in inst.note and "03:30" in inst.note


def test_fall_back_overlap_uses_first_instant():
    # UK clocks go 02:00 BST -> 01:00 GMT on 2026-10-25; 01:30 happens twice.
    inst = localize(date(2026, 10, 25), time(1, 30), ZoneInfo("Europe/London"))
    assert inst.utc.isoformat() == "2026-10-25T00:30:00+00:00"  # the BST one
    assert "occurs twice" in inst.note
