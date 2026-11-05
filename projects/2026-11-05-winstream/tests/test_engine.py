import pytest

from helpers import drive, ev, materialise
from winstream.engine import Operator
from winstream.model import WindowSpec

T60 = WindowSpec("tumbling", size=60)
S30 = WindowSpec("session", gap=30)


def kinds(out):
    return [(r["kind"], r["key"], r["wstart"], r["wend"], r["revision"], r["count"]) for r in out]


def test_window_fires_only_when_watermark_passes_end():
    op = Operator(T60, delay=10, lateness=0)
    out, _ = op.process(ev("1", "k", 5), 0)
    assert out == []
    out, _ = op.process(ev("2", "k", 69), 1)        # wm = 59 < 60
    assert out == []
    out, _ = op.process(ev("3", "k", 70), 2)        # wm = 60 >= 60 -> fire [0,60)
    assert kinds(out) == [("insert", "k", 0, 60, 0, 1)]
    assert out[0]["wm"] == 60 and out[0]["trigger_offset"] == 2


def test_late_event_within_lateness_updates_then_beyond_is_dropped():
    op, out, side = drive(T60, 0, 30, [
        ev("1", "k", 10), ev("2", "k", 61),          # wm 61: fires [0,60)
        ev("3", "k", 20),                            # 60 + 30 > 61 -> accepted, update
        ev("4", "k", 95),                            # wm 95 > 90 -> [0,60) purged
        ev("5", "k", 30),                            # dropped
    ])
    assert kinds(out)[:2] == [("insert", "k", 0, 60, 0, 1), ("update", "k", 0, 60, 1, 2)]
    assert [(s["reason"], s["event_id"], s["wstart"]) for s in side] == [("late", "5", 0)]
    assert materialise(out)[("k", 0, 60)]["count"] == 2
    assert op.stats["pairs_late"] == 1 and op.stats["updates"] == 1


def test_lateness_zero_drops_exactly_at_boundary():
    _, out, side = drive(T60, 0, 0, [ev("1", "k", 60), ev("2", "k", 59)])
    assert [s["event_id"] for s in side] == ["2"]   # end 60 + 0 <= wm 60
    assert kinds(out) == [("insert", "k", 60, 120, 0, 1)]


def test_duplicates_are_idempotent():
    events = [ev("1", "k", 10, 7), ev("1", "k", 10, 7), ev("2", "k", 70), ev("1", "k", 10, 7)]
    _, out, side = drive(T60, 0, 100, events)
    assert materialise(out)[("k", 0, 60)] == {"count": 1, "sum": 7, "min": 7, "max": 7, "mean": 7.0}
    assert [s["reason"] for s in side] == ["duplicate", "duplicate"]
    assert all(r["kind"] == "insert" for r in out), "a duplicate must not emit an update"


def test_hopping_event_counted_in_every_window():
    spec = WindowSpec("hopping", size=60, slide=20)
    _, out, _ = drive(spec, 0, 0, [ev("1", "k", 45)])
    assert sorted(materialise(out)) == [("k", 0, 60), ("k", 20, 80), ("k", 40, 100)]


def test_keys_are_independent_but_share_the_watermark():
    _, out, side = drive(T60, 0, 0, [ev("1", "a", 10), ev("2", "b", 200), ev("3", "a", 20)])
    assert [s["event_id"] for s in side] == ["3"]   # global watermark is 200
    assert ("a", 0, 60) in materialise(out)


def test_session_merge_of_two_emitted_sessions_retracts_both():
    _, out, side = drive(S30, 0, 1000, [
        ev("1", "k", 0), ev("2", "k", 50),           # sessions [0,30) and [50,80)
        ev("3", "k", 200),                           # wm 200 fires both
        ev("4", "k", 25),                            # bridges: [0,55) overlaps [50,80)
    ])
    assert kinds(out)[:3] == [("insert", "k", 0, 30, 0, 1), ("insert", "k", 50, 80, 0, 1),
                              ("retract", "k", 0, 30, 0, 1)]
    assert ("retract", "k", 50, 80, 0, 1) in kinds(out)
    assert ("insert", "k", 0, 80, 0, 3) in kinds(out)
    assert materialise(out) == {
        ("k", 0, 80): {"count": 3, "sum": 3, "min": 1, "max": 1, "mean": 1.0},
        ("k", 200, 230): {"count": 1, "sum": 1, "min": 1, "max": 1, "mean": 1.0}}
    assert side == []


def test_session_event_inside_live_session_is_not_late_even_behind_watermark():
    _, out, side = drive(S30, 0, 0, [
        ev("1", "k", 0), ev("2", "k", 20), ev("3", "k", 40),    # session [0,70), wm 40
        ev("4", "k", 10),                                       # 10 < wm but inside [0, 40]
        ev("5", "k", 45),
        ev("6", "j", 69),                                       # wm 69
        ev("7", "k", 41),                                       # inside [0, 45], still held
        ev("8", "k", 2),                                        # inside
    ])
    assert side == []
    assert materialise(out)[("k", 0, 75)]["count"] == 7


def test_session_event_that_would_extend_behind_watermark_is_dropped():
    _, out, side = drive(S30, 0, 0, [ev("1", "k", 100), ev("2", "k", 150), ev("3", "k", 90)])
    assert [(s["reason"], s["event_id"]) for s in side] == [("late", "3")]
    assert ("k", 100, 130) in materialise(out)


def test_session_update_in_place_keeps_identity():
    _, out, _ = drive(S30, 0, 100, [ev("1", "k", 0), ev("2", "k", 20), ev("3", "k", 100),
                                    ev("4", "k", 10)])
    assert ("update", "k", 0, 50, 1, 3) in kinds(out)


def test_flush_emits_everything_and_rejects_more_input():
    op, out, _ = drive(T60, 1000, 0, [ev("1", "k", 10), ev("2", "k", 100)])
    assert len(out) == 2 and all(r["wm"] is None for r in out)
    assert op.open_windows() == 0
    with pytest.raises(RuntimeError):
        op.process(ev("3", "k", 1))


@pytest.mark.parametrize("spec", [T60, S30, WindowSpec("hopping", size=60, slide=20)])
def test_state_snapshot_resume_is_identical(spec):
    events = [ev(str(i), "k%d" % (i % 3), (i * 37) % 400 + i, i, None) for i in range(120)]
    _, want, want_side = drive(spec, 15, 40, events)
    op = Operator(spec, 15, 40)
    got, got_side = [], []
    for i, e in enumerate(events):
        if i % 17 == 0:
            op = Operator.from_state(spec, op.to_state())
        o, s = op.process(e, i)
        got.extend(o)
        got_side.extend(s)
    got.extend(op.flush())
    assert got == want and got_side == want_side


def test_from_state_rejects_other_window():
    op = Operator(T60, 0, 0)
    with pytest.raises(ValueError, match="checkpoint was taken"):
        Operator.from_state(S30, op.to_state())


def test_negative_parameters_rejected():
    with pytest.raises(ValueError):
        Operator(T60, -1, 0)
