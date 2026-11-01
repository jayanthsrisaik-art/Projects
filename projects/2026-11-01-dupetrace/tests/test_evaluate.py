import json
from pathlib import Path

import pytest

from dupetrace import evaluate, store, synth

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _load(**kw):
    reports, labels = synth.generate(**kw)
    conn = store.connect(":memory:")
    store.ingest_lines(conn, (json.dumps(r) for r in reports))
    return store.load_reports(conn), labels


def test_generator_is_deterministic_and_labels_are_separate():
    a, la = synth.generate(n_bugs=40, seed=3)
    b, lb = synth.generate(n_bugs=40, seed=3)
    assert a == b and la == lb
    assert all(set(r) == {"id", "created_at", "title", "body", "component", "version"} for r in a)
    assert set(la) == {r["id"] for r in a}


def test_replay_never_offers_future_reports():
    reports, labels = _load(n_bugs=60, seed=5)
    order = {r.id: i for i, r in enumerate(reports)}
    records = evaluate.replay(reports, labels)
    for rec in records:
        qi = order[rec.report.id]
        assert all(order[c.report.id] < qi for c in rec.norm)
        assert all(order[i] < qi for i, _ in rec.raw)
        assert all(order[i] < qi for i, _ in rec.signature)
        assert all(order[i] < qi for i in rec.truth)
    splits = [r.split for r in records]
    assert splits == sorted(splits, key=["train", "val", "test"].index)


def test_ranking_and_decision_metrics_hand_computed():
    ranked = [["a", "b"], ["x", "y"], ["z"], ["n"]]
    truth = [{"a"}, {"y"}, {"q"}, set()]
    m = evaluate.ranking_metrics(ranked, truth)
    assert m["recall@1"] == pytest.approx(1 / 3)
    assert m["recall@5"] == pytest.approx(2 / 3)
    assert m["mrr"] == pytest.approx((1 + 0.5 + 0) / 3)
    top = [("a", 0.9), ("x", 0.8), ("z", 0.2), ("n", 0.95)]
    d = evaluate.decision_metrics(top, truth, threshold=0.5)
    assert d["suggested"] == 3 and d["precision"] == pytest.approx(1 / 3)
    assert d["recall"] == pytest.approx(1 / 3) and d["false_alarm_rate_on_new"] == 1.0


def test_best_threshold_picks_f1_optimum():
    top = [("a", 0.9), ("x", 0.3), ("n", 0.5)]
    truth = [{"a"}, {"x"}, set()]
    # t=0.9 -> P=1,R=.5,F1=.667 ; t=0.5 -> P=.5,R=.5 ; t=0.3 -> P=.667,R=1,F1=.8
    assert evaluate.best_threshold(top, truth) == 0.3


def test_reranker_beats_baselines_on_small_synthetic_run():
    reports, labels = _load(n_bugs=200, seed=11)
    rep, model = evaluate.run(reports, labels)
    m = rep["methods"]
    assert m["reranker"]["ranking"]["recall@1"] > m["bm25_normalised"]["ranking"]["recall@1"]
    assert m["bm25_normalised"]["ranking"]["recall@1"] > m["bm25_raw"]["ranking"]["recall@1"]
    assert m["reranker"]["decision"]["f1"] > m["bm25_normalised"]["decision"]["f1"]
    assert 0 < model.threshold < 1


def test_default_pipeline_reproduces_committed_metrics():
    """The numbers in samples/expected_metrics.json (quoted in the README) come from this exact run."""
    reports, labels = _load()  # default n_bugs=420, seed=7
    rep, _ = evaluate.run(reports, labels)
    expected = json.loads((SAMPLES / "expected_metrics.json").read_text())
    assert rep["data"] == expected["data"]
    for name, m in expected["methods"].items():
        for k, v in m["ranking"].items():
            assert rep["methods"][name]["ranking"][k] == pytest.approx(v, abs=1e-9), (name, k)
        for k, v in m["decision"].items():
            assert rep["methods"][name]["decision"][k] == pytest.approx(v, abs=1e-6), (name, k)
