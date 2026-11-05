import pytest

from winstream.model import Acc, Event, InvalidEvent, WindowSpec, parse_event, parse_window


def test_parse_event_epoch_and_iso():
    e = parse_event('{"id":"x","key":"k","ts":60,"value":3,"arrival":61}')
    assert e == Event("x", "k", 60, 3, 61)
    e2 = parse_event('{"id":"x","key":"k","ts":"1970-01-01T01:00:00+01:00","value":3}')
    assert e2.ts == 0 and e2.arrival is None


@pytest.mark.parametrize("line,reason", [
    ("nope", "bad json"),
    ("[1]", "not a JSON object"),
    ('{"id":"x","key":"k","ts":1}', "missing field 'value'"),
    ('{"id":"","key":"k","ts":1,"value":1}', "id must be"),
    ('{"id":"x","key":"k","ts":1,"value":1.5}', "value must be an integer"),
    ('{"id":"x","key":"k","ts":1,"value":true}', "value must be an integer"),
    ('{"id":"x","key":"k","ts":"2026-01-01T00:00:00","value":1}', "explicit offset"),
    ('{"id":"x","key":"k","ts":true,"value":1}', "got bool"),
])
def test_parse_event_rejects(line, reason):
    with pytest.raises(InvalidEvent, match=reason):
        parse_event(line)


def test_tumbling_assign_half_open():
    w = WindowSpec("tumbling", size=60)
    assert w.assign(0) == [(0, 60)]
    assert w.assign(59) == [(0, 60)]
    assert w.assign(60) == [(60, 120)]
    assert w.assign(-1) == [(-60, 0)]


def test_hopping_assign_covers_every_containing_window():
    w = WindowSpec("hopping", size=300, slide=60)
    got = w.assign(130)
    assert got == [(-120, 180), (-60, 240), (0, 300), (60, 360), (120, 420)]
    assert all(s <= 130 < e for s, e in got)


def test_parse_window_and_validation():
    assert parse_window("session:30") == WindowSpec("session", gap=30)
    assert parse_window("hopping:300:60").slide == 60
    for bad in ("tumbling", "tumbling:x", "hopping:300", "sliding:5", "tumbling:0"):
        with pytest.raises(ValueError):
            parse_window(bad)
    with pytest.raises(ValueError, match="multiple"):
        WindowSpec("hopping", size=100, slide=30)


def test_acc_merge_and_roundtrip():
    a, b = Acc(), Acc()
    a.add(Event("1", "k", 0, 5))
    assert not a.add(Event("1", "k", 0, 5)), "same id must be ignored"
    b.add(Event("2", "k", 0, -3))
    a.merge(b)
    assert a.values() == {"count": 2, "sum": 2, "min": -3, "max": 5, "mean": 1.0}
    assert Acc.from_state(a.to_state()).values() == a.values()
