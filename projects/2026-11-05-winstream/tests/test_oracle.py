"""Randomised cross-check: streaming operator vs the independent batch oracle."""
import random

import pytest

from helpers import drive, materialise
from winstream.generator import generate
from winstream.model import Event, WindowSpec
from winstream.oracle import _fixed_windows, compare, expected, truth

SPECS = [WindowSpec("tumbling", size=60), WindowSpec("tumbling", size=7),
         WindowSpec("hopping", size=90, slide=30), WindowSpec("session", gap=45),
         WindowSpec("session", gap=5)]


def random_stream(seed, n=300):
    """Small-domain, very disordered stream with duplicates: many collisions."""
    rng = random.Random(seed)
    evs = []
    for i in range(n):
        ts = rng.randint(0, 900)
        e = Event(f"e{i}", rng.choice("abc"), ts, rng.randint(-50, 50), ts + int(rng.expovariate(1 / 40)))
        evs.append(e)
        if rng.random() < 0.1:
            evs.append(Event(e.id, e.key, e.ts, e.value, e.arrival + rng.randint(0, 200)))
    evs.sort(key=lambda e: (e.arrival, e.id))
    return evs


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.describe())
@pytest.mark.parametrize("delay,lateness", [(0, 0), (10, 0), (0, 30), (25, 120), (5, 5000)])
@pytest.mark.parametrize("seed", range(4))
def test_operator_matches_oracle(spec, delay, lateness, seed):
    events = random_stream(seed)
    _, out, side = drive(spec, delay, lateness, events)
    want, want_late = expected(events, spec, delay, lateness)
    diff = compare(materialise(out), want)
    assert diff["ok"], diff
    got_late = sorted((s["event_id"], s["wstart"], s["wend"]) for s in side if s["reason"] == "late")
    assert got_late == want_late


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.describe())
def test_unbounded_lateness_converges_to_batch_truth(spec):
    events = random_stream(11)
    _, out, side = drive(spec, 0, 10 ** 9, events)
    assert not [s for s in side if s["reason"] == "late"]
    assert compare(materialise(out), truth(events, spec))["ok"]


def test_generated_stream_has_real_disorder_and_duplicates():
    events = generate(n_keys=4, duration=3600, seed=3)
    inversions = sum(1 for a, b in zip(events, events[1:]) if b.ts < a.ts)
    ids = [e.id for e in events]
    assert inversions > 50
    assert len(ids) > len(set(ids))
    assert events == sorted(events, key=lambda e: (e.arrival, e.key, e.id))
    assert generate(n_keys=4, duration=3600, seed=3) == events, "generator must be deterministic"


def test_oracle_window_assignment_brute_force():
    for ts in range(-200, 200, 7):
        for size, slide in ((60, 60), (90, 30), (7, 7)):
            kind = "tumbling" if size == slide else "hopping"
            want = [(s, s + size) for s in range(-400, 400) if s % slide == 0 and s <= ts < s + size]
            assert _fixed_windows(kind, size, slide, ts) == want


def test_oracle_sessions_split_on_gap_boundary():
    evs = [Event("1", "k", 0, 1), Event("2", "k", 10, 1), Event("3", "k", 20, 1)]
    assert sorted(truth(evs, WindowSpec("session", gap=10))) == [("k", 0, 10), ("k", 10, 20), ("k", 20, 30)]
    assert sorted(truth(evs, WindowSpec("session", gap=11))) == [("k", 0, 31)]
