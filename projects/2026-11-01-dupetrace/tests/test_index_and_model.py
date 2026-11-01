import numpy as np
import pytest

from dupetrace.features import FEATURES, pair_features
from dupetrace.index import BM25Index
from dupetrace.model import Reranker
from dupetrace.store import build_report


def rep(i, title, body="body", t="2026-01-01T00:00:00Z", **kw):
    return build_report({"id": i, "created_at": t, "title": title, "body": body, **kw})


def test_bm25_prefers_matching_document_and_ties_by_recency():
    idx = BM25Index()
    idx.add(rep("a", "coupon crash checkout", "x"))
    idx.add(rep("b", "avatar upload slow", "y"))
    idx.add(rep("c", "coupon crash checkout", "x"))
    hits = idx.search(rep("q", "coupon crash", "z"))
    assert [h[0].id for h in hits[:2]] == ["c", "a"]  # identical scores -> newer first
    assert all(h[0].id != "b" for h in hits)


def test_idf_uses_only_documents_seen_so_far():
    idx = BM25Index()
    idx.add(rep("a", "alpha beta"))
    before = idx.idf("alpha")
    idx.add(rep("b", "alpha gamma"))
    assert idx.idf("alpha") < before


def test_self_score_bounds_candidate_score():
    idx = BM25Index()
    q = rep("q", "coupon crash checkout")
    idx.add(rep("a", "coupon crash checkout"))
    idx.add(rep("b", "something else entirely"))
    (top, s), *_ = idx.search(q)
    assert top.id == "a"
    assert s == pytest.approx(idx.self_score(q))


def test_pair_features_shape_and_conflicts():
    q = rep("q", "x", "KeyError: 'a'", component="checkout", version="3.4.0")
    c = rep("c", "x", "ValueError: bad", component="auth", version="3.6.1", t="2025-12-30T00:00:00Z")
    f = dict(zip(FEATURES, pair_features(q, c, 1.0, 2.0, 2, 4.0)))
    assert len(f) == len(FEATURES)
    assert f["exc_conflict"] == 1 and f["exc_match"] == 0
    assert f["component_conflict"] == 1 and f["version_gap"] == 2
    assert f["bm25_rel"] == 0.5 and f["bm25_self"] == 0.25 and f["inv_rank"] == 0.5


def test_unknown_component_is_neither_match_nor_conflict():
    q = rep("q", "x", component="unknown")
    c = rep("c", "x", component="auth")
    f = dict(zip(FEATURES, pair_features(q, c, 1, 1, 1, 1)))
    assert f["component_match"] == 0 and f["component_conflict"] == 0


def test_reranker_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, len(FEATURES)))
    y = (X[:, 0] + 0.1 * rng.normal(size=200) > 0).astype(int)
    m = Reranker.fit(X, y)
    m.threshold = 0.7
    m.save(tmp_path / "m.json")
    m2 = Reranker.load(tmp_path / "m.json")
    assert np.allclose(m.score(X), m2.score(X))
    assert m2.threshold == 0.7
    assert m.weights()[0][0] == FEATURES[0]


def test_reranker_requires_both_classes():
    with pytest.raises(ValueError):
        Reranker.fit(np.zeros((5, len(FEATURES))), np.zeros(5, dtype=int))


def test_model_load_rejects_feature_mismatch(tmp_path):
    p = tmp_path / "m.json"
    p.write_text('{"features": ["x"], "mean": [0], "scale": [1], "coef": [1], "intercept": 0, "threshold": 0.5}')
    with pytest.raises(ValueError):
        Reranker.load(p)
