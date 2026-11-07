import json

from semguard.cache import CacheConfig
from semguard.datagen import FAMILIES, generate
from semguard.evaluate import calibrate, replay, run_eval, split_by_topic


def test_generator_is_deterministic_and_labelled():
    a, b = generate(300, seed=3), generate(300, seed=3)
    assert a == b and a != generate(300, seed=4)
    fams = {f.id: f for f in FAMILIES}
    for r in a:
        assert r["key"].split("|")[0] == r["family"] and fams[r["family"]].topic == r["topic"]


def test_split_is_group_disjoint_and_deterministic():
    recs = generate(1000)
    dev, test = split_by_topic(recs)
    assert {r["topic"] for r in dev}.isdisjoint({r["topic"] for r in test})
    assert len(dev) + len(test) == len(recs)
    assert split_by_topic(recs) == (dev, test)
    # volatile intents appear on both sides, so calibration sees them
    assert any(r["volatile"] for r in dev) and any(r["volatile"] for r in test)


def test_replay_accounting():
    recs = generate(600)
    m = replay(recs, CacheConfig())
    assert m["hits"] == m["correct_hits"] + m["wrong_hits"]
    assert m["llm_calls"] == m["queries"] - m["hits"]
    assert m["correct_hits"] <= m["reusable"]
    assert sum(m["wrong_by_kind"].values()) == m["wrong_hits"]


def test_exact_match_never_wrong_except_stale():
    m = replay(generate(800), CacheConfig(semantic=False, guards=()))
    assert set(m["wrong_by_kind"]) <= {"stale_volatile"}


def test_calibration_uses_only_dev():
    recs = generate(1200)
    dev, test = split_by_topic(recs)
    base = CacheConfig()
    grid = [0.6, 0.7, 0.8, 0.9]
    c1 = calibrate(dev, base, grid=grid)
    garbage_test = [dict(r, key="x") for r in test]  # test labels must not matter
    c2 = calibrate(split_by_topic(dev + garbage_test)[0], base, grid=grid)
    assert c1 == c2


def test_full_eval_matches_committed_expected_output():
    rep = run_eval(generate(3000, 7))
    with open("samples/expected_eval.json") as fh:
        expected = json.load(fh)
    assert rep == expected
    g, s = rep["policies"]["guarded"]["test"], rep["policies"]["semantic_only"]["test"]
    assert g["hit_precision"] > s["hit_precision"] and g["wrong_hits"] < s["wrong_hits"]
