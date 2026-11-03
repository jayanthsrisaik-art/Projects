import pytest

from cronwhen import CronSyntaxError, parse


def test_basic_fields_and_steps():
    s = parse("*/15 9-17/4 1,15 * *")
    assert s.minutes == {0, 15, 30, 45}
    assert s.hours == {9, 13, 17}
    assert s.dom == {1, 15}
    assert s.months == set(range(1, 13))
    assert not s.dom_star and s.dow_star


def test_start_step_shorthand_runs_to_field_max():
    assert parse("5/20 * * * *").minutes == {5, 25, 45}


def test_names_case_insensitive_and_sunday_aliases():
    s = parse("0 0 * jan-MAR,Dec sat-7")
    assert s.months == {1, 2, 3, 12}
    assert s.dow == {6, 0}
    assert parse("0 0 * * 7").dow == {0}
    assert parse("0 0 * * 5-7").dow == {5, 6, 0}


def test_macros():
    assert parse("@daily").raw_fields == ("0", "0", "*", "*", "*")
    assert parse("@weekly").dow == {0}
    assert parse("@HOURLY").hours == set(range(24))


def test_special_tokens():
    s = parse("0 0 L,L-3,15W,LW * *")
    assert s.dom_last == {0, 3}
    assert s.dom_weekday == {15}
    assert s.dom_last_weekday
    d = parse("0 0 * * 5L,FRI#3,mon#1")
    assert d.dow_last == {5}
    assert d.dow_nth == {(5, 3), (1, 1)}


def test_star_flags_follow_vixie():
    s = parse("*/5 * */2 * MON")
    assert s.minute_star and s.hour_star and s.dom_star and not s.dow_star
    assert s.is_interval
    assert not parse("30 2 * * *").is_interval
    assert parse("*/10 2 * * *").is_interval  # minute wildcard counts


@pytest.mark.parametrize("expr,field,fragment", [
    ("60 * * * *", "minute", "out of range 0-59"),
    ("0 24 * * *", "hour", "out of range 0-23"),
    ("0 0 0 * *", "day-of-month", "out of range 1-31"),
    ("0 0 * 13 *", "month", "out of range"),
    ("0 0 * * 8", "day-of-week", "out of range"),
    ("*/0 * * * *", "minute", "positive integer"),
    ("5-1 * * * *", "minute", "range start is after range end"),
    ("0 0 * * FRI#6", "day-of-week", "1-5"),
    ("0 0 * * FOO", "day-of-week", "number or name"),
    ("0 0 1,,2 * *", "day-of-month", "empty list element"),
    ("? * * * *", "minute", "only allowed in day fields"),
    ("0 0 L-31 * *", "day-of-month", "offset"),
    ("0 0 2L * *", "day-of-month", "unsupported"),
])
def test_errors_name_field_and_token(expr, field, fragment):
    with pytest.raises(CronSyntaxError) as ei:
        parse(expr)
    assert ei.value.field_name == field
    assert fragment in str(ei.value)


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "@reboot", "@fortnightly"])
def test_whole_expression_errors(expr):
    with pytest.raises(CronSyntaxError):
        parse(expr)


def test_dom_dow_mode_validated():
    with pytest.raises(ValueError):
        parse("* * * * *", dom_dow="xor")
